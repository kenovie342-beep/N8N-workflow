"""Persistent job queue with enforced concurrency, retries, recovery and cancellation."""
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from . import github_runner, http, store, tasks
from .config import env_int
from .db import session
from .logsafe import scrub
from .models import Job, JobLog, iso, utcnow

log = logging.getLogger("suite.jobs")
FINAL = ("completed", "failed", "cancelled")
LOCAL_LEASE = 600
_claim_lock = threading.Lock()


def add_log(db, job_id: str, message: str, level: str = "info") -> None:
    db.add(JobLog(job_id=job_id, level=level, message=scrub(str(message))[:4000]))
    db.commit()


def backoff(attempts: int, retry_after: float | None = None) -> timedelta:
    base = min(5 * (2 ** max(0, attempts - 1)), 900)
    return timedelta(seconds=max(base, retry_after or 0))


# ------------------------------------------------------------------- enqueue
def enqueue(db, task: str, payload: dict | None = None, *, priority: int = 5, max_attempts: int = 3,
            dedupe_key: str | None = None, run_after=None, schedule_id: str | None = None,
            workflow: str = "", commit: bool = True) -> tuple[Job, bool]:
    """Create a job. Returns (job, created). With a dedupe_key an active duplicate is returned instead."""
    if dedupe_key:
        existing = db.scalars(select(Job).where(Job.dedupe_key == dedupe_key, Job.status.in_(("queued", "running")))).first()
        if existing:
            return existing, False
    job = Job(task=task, payload_json=json.dumps(payload or {}), priority=priority, max_attempts=max_attempts,
              dedupe_key=dedupe_key, run_after=run_after or utcnow(), schedule_id=schedule_id, workflow=workflow,
              executor="github" if task == "github_job" else "local")
    db.add(job)
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except IntegrityError:  # lost a race on the unique active dedupe index
        db.rollback()
        existing = db.scalars(select(Job).where(Job.dedupe_key == dedupe_key, Job.status.in_(("queued", "running")))).first()
        if existing:
            return existing, False
        raise
    return job, True


# --------------------------------------------------------------------- claim
def claim(db, now=None) -> list[Job]:
    """Atomically move up to (max_concurrency - running) queued jobs to running."""
    now = now or utcnow()
    with _claim_lock:
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(727001)"))  # serialises claimers across processes
        cap = store.get_max_concurrency(db) - db.scalar(select(func.count()).select_from(Job).where(Job.status == "running"))
        if cap <= 0:
            db.rollback()
            return []
        q = (select(Job).where(Job.status == "queued", Job.run_after <= now)
             .order_by(Job.priority.desc(), Job.created_at.asc()).limit(cap))
        if db.bind.dialect.name == "postgresql":
            q = q.with_for_update(skip_locked=True)
        rows = list(db.scalars(q))
        for j in rows:
            j.status, j.attempts = "running", j.attempts + 1
            j.started_at, j.updated_at = now, now
            j.lease_expires_at = now + timedelta(seconds=LOCAL_LEASE)
            j.cancel_requested = False
        db.commit()
        return rows


# -------------------------------------------------------------- state changes
def complete(db, job_id: str, result: dict | None = None) -> bool:
    """running -> completed. Idempotent: returns False if the job was not running (duplicate callback)."""
    now = utcnow()
    n = db.execute(update(Job).where(Job.id == job_id, Job.status == "running")
                   .values(status="completed", result_json=json.dumps(result or {}), error="",
                           finished_at=now, updated_at=now, lease_expires_at=None)).rowcount
    db.commit()
    return bool(n)


def fail(db, job_id: str, error: str, *, retryable: bool = True, retry_after: float | None = None) -> str | None:
    """running -> queued (retry with backoff) or failed. Returns new status, or None if not running."""
    job = db.get(Job, job_id)
    if not job or job.status != "running":
        return None
    now = utcnow()
    job.error = scrub(error)[:1500]
    job.updated_at, job.lease_expires_at = now, None
    if retryable and job.attempts < job.max_attempts:
        job.status, job.run_after = "queued", now + backoff(job.attempts, retry_after)
    else:
        job.status, job.finished_at = "failed", now
    db.commit()
    add_log(db, job_id, f"{job.status}: {job.error}", "error")
    return job.status


def cancel(db, job_id: str) -> str:
    job = db.get(Job, job_id)
    if not job:
        raise LookupError("Job not found")
    if job.status in FINAL:
        return job.status
    now = utcnow()
    if job.status == "queued":
        job.status, job.finished_at, job.updated_at = "cancelled", now, now
        db.commit()
        return "cancelled"
    # running
    job.cancel_requested = True
    if job.executor == "github":
        try:
            cfg = store.github_config(db)
            run_id = job.remote_run_id
            if not run_id:
                run = github_runner.find_run(cfg, job.id)
                run_id = str(run["id"]) if run else ""
            if run_id:
                github_runner.cancel_run(cfg, run_id)
            job.status, job.finished_at, job.updated_at = "cancelled", now, now
            job.lease_expires_at = None
        except (http.RetryableError, http.PermanentError) as exc:
            job.error = f"Remote cancel failed: {exc}"[:500]
    db.commit()
    return job.status  # local running jobs stop cooperatively; see Worker


def retry(db, job_id: str) -> None:
    job = db.get(Job, job_id)
    if not job:
        raise LookupError("Job not found")
    if job.status not in ("failed", "cancelled"):
        raise ValueError("Only failed or cancelled jobs can be retried")
    job.status, job.attempts, job.error, job.finished_at = "queued", 0, "", None
    job.run_after, job.updated_at, job.cancel_requested = utcnow(), utcnow(), False
    db.commit()


def recover_expired(db, now=None, force_local: bool = False) -> int:
    """Interrupted executions (crash/restart/lost callback): lease expired -> retry or fail."""
    now = now or utcnow()
    q = select(Job).where(Job.status == "running")
    n = 0
    for j in db.scalars(q).all():
        expired = j.lease_expires_at is not None and j.lease_expires_at < now
        if expired or (force_local and j.executor == "local"):
            if fail(db, j.id, "Interrupted: execution lease expired (worker restart or lost callback)") is not None:
                n += 1
    return n


def reconcile_github(db, min_age: int = 90) -> int:
    """Failure detection for lost callbacks: ask GitHub whether the run already ended."""
    cfg = store.github_config(db)
    if not cfg.get("token"):
        return 0
    n = 0
    cutoff = utcnow() - timedelta(seconds=min_age)
    for j in db.scalars(select(Job).where(Job.status == "running", Job.executor == "github", Job.started_at < cutoff)).all():
        try:
            run = github_runner.find_run(cfg, j.id)
        except (http.RetryableError, http.PermanentError):
            continue
        if not run:
            continue
        if not j.remote_run_id:
            j.remote_run_id = str(run["id"])
            db.commit()
        if run.get("status") == "completed" and run.get("conclusion") != "success":
            if fail(db, j.id, f"GitHub run ended with conclusion '{run.get('conclusion')}' without a callback") is not None:
                n += 1
    return n


# -------------------------------------------------------------------- worker
class Worker:
    """Polls the DB, claims jobs (bounded by max_concurrency) and executes them on a thread pool."""

    def __init__(self, runner=tasks.run_task, poll: float = 1.0, with_scheduler: bool = True):
        self.runner, self.poll, self.with_scheduler = runner, poll, with_scheduler
        self.pool = ThreadPoolExecutor(max_workers=128, thread_name_prefix="suite-job")  # ceiling only
        self.owned: dict[str, threading.Event] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_reconcile = 0.0

    # lifecycle
    def start(self) -> None:
        db = session()
        try:
            n = recover_expired(db, force_local=True)  # anything "running" locally died with the old process
            if n:
                log.warning("Recovered %d interrupted job(s)", n)
        finally:
            db.close()
        self._thread = threading.Thread(target=self._loop, name="suite-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.pool.shutdown(wait=False, cancel_futures=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("worker tick failed")
            self._stop.wait(self.poll)

    # one scheduling cycle (public so tests can drive it deterministically)
    def tick(self) -> int:
        db = session()
        try:
            if self.with_scheduler:
                from . import scheduler
                scheduler.tick(db)
            recover_expired(db)
            self._heartbeat_and_cancel(db)
            if time.monotonic() - self._last_reconcile > 30:
                self._last_reconcile = time.monotonic()
                reconcile_github(db)
            started = claim(db)
            for j in started:
                ev = threading.Event()
                self.owned[j.id] = ev
                self.pool.submit(self._run, j.id, j.task, json.loads(j.payload_json), ev)
            return len(started)
        finally:
            db.close()

    def _heartbeat_and_cancel(self, db) -> None:
        if not self.owned:
            return
        ids = list(self.owned)
        db.execute(update(Job).where(Job.id.in_(ids), Job.status == "running")
                   .values(lease_expires_at=utcnow() + timedelta(seconds=LOCAL_LEASE)))
        db.commit()
        for jid in db.scalars(select(Job.id).where(Job.id.in_(ids), Job.cancel_requested == True)):  # noqa: E712
            self.owned[jid].set()

    def _run(self, job_id: str, task: str, payload: dict, ev: threading.Event) -> None:
        db = session()
        try:
            ctx = tasks.Ctx(job_id, ev, lambda m, level="info": add_log(db, job_id, m, level))
            try:
                result = self.runner(task, payload, ctx)
            except http.RetryableError as exc:
                fail(db, job_id, str(exc), retryable=True, retry_after=exc.retry_after)
                return
            except http.PermanentError as exc:
                fail(db, job_id, str(exc), retryable=False)
                return
            except (LookupError, ValueError, KeyError) as exc:
                fail(db, job_id, f"{type(exc).__name__}: {exc}", retryable=False)
                return
            except Exception as exc:
                fail(db, job_id, f"{type(exc).__name__}: {exc}", retryable=True)
                return
            if isinstance(result, tasks.Deferred):
                db.execute(update(Job).where(Job.id == job_id, Job.status == "running").values(
                    executor="github", lease_expires_at=utcnow() + timedelta(seconds=result.timeout_seconds)))
                db.commit()
                return
            if ev.is_set() or (isinstance(result, dict) and result.get("cancelled")):
                now = utcnow()
                db.execute(update(Job).where(Job.id == job_id, Job.status == "running").values(
                    status="cancelled", finished_at=now, updated_at=now, lease_expires_at=None))
                db.commit()
                return
            complete(db, job_id, result if isinstance(result, dict) else {"result": result})
        except Exception:
            log.exception("job runner crashed")
        finally:
            self.owned.pop(job_id, None)
            db.close()


# --------------------------------------------------------------------- views
def job_dict(j: Job, full: bool = False) -> dict:
    d = {"id": j.id, "task": j.task, "workflow": j.workflow, "status": j.status, "priority": j.priority,
         "attempts": j.attempts, "max_attempts": j.max_attempts, "error": j.error, "executor": j.executor,
         "schedule_id": j.schedule_id, "created_at": iso(j.created_at), "started_at": iso(j.started_at),
         "finished_at": iso(j.finished_at), "run_after": iso(j.run_after),
         "duration_seconds": round((j.finished_at - j.started_at).total_seconds(), 2) if j.finished_at and j.started_at else None}
    if full:
        d["result"] = json.loads(j.result_json or "{}")
        d["payload"] = _safe_payload(json.loads(j.payload_json or "{}"))
        d["remote_run_id"] = j.remote_run_id
    return d


def _safe_payload(p: dict) -> dict:
    return {k: ("***" if any(s in k.lower() for s in ("key", "secret", "token", "password")) else v) for k, v in p.items()}


def stats(db) -> dict:
    counts = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    done = db.scalars(select(Job).where(Job.status == "completed", Job.started_at.is_not(None))
                      .order_by(Job.finished_at.desc()).limit(200)).all()
    durs = [(j.finished_at - j.started_at).total_seconds() for j in done if j.finished_at]
    recent = db.scalars(select(Job).order_by(Job.updated_at.desc()).limit(10)).all()
    return {"total": sum(counts.values()), "queued": counts.get("queued", 0), "running": counts.get("running", 0),
            "completed": counts.get("completed", 0), "failed": counts.get("failed", 0),
            "cancelled": counts.get("cancelled", 0), "max_concurrency": store.get_max_concurrency(db),
            "avg_duration_seconds": round(sum(durs) / len(durs), 2) if durs else None,
            "recent": [job_dict(j) for j in recent]}


__all__ = ["env_int"]
