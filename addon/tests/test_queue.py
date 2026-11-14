import threading
import time

import pytest
from datetime import timedelta

from sqlalchemy import func, select

from app import db as dbmod, jobs, store
from app.http import RetryableError
from app.models import Job, utcnow


def wait_for(cond, timeout=20.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def count(status):
    s = dbmod.session()
    try:
        return s.scalar(select(func.count()).select_from(Job).where(Job.status == status))
    finally:
        s.close()


class Probe:
    """Runner that records real simultaneous execution."""

    def __init__(self, hold=0.4):
        self.hold, self.lock, self.active, self.peak, self.done = hold, threading.Lock(), 0, 0, 0

    def __call__(self, task, payload, ctx):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.hold)
        finally:
            with self.lock:
                self.active -= 1
                self.done += 1
        return {"ok": True}


def test_concurrency_limit_five_with_twelve_jobs(db):
    """Limit=5, 12 jobs: never >5 simultaneous (measured in-process AND via DB), rest stay queued, all finish."""
    store.set_app(db, "max_concurrency", "5")
    for i in range(12):
        jobs.enqueue(db, "noop", {"i": i})
    probe, w = Probe(hold=0.5), None
    w = jobs.Worker(runner=probe, poll=0.02, with_scheduler=False)
    db_peak, queued_seen = 0, False
    w.start()
    try:
        end = time.time() + 30
        while time.time() < end and count("completed") < 12:
            running, queued = count("running"), count("queued")
            db_peak = max(db_peak, running)
            if running == 5 and queued >= 1:
                queued_seen = True
            time.sleep(0.01)
    finally:
        w.stop()
    assert count("completed") == 12, "jobs were lost or never ran"
    assert probe.peak <= 5, f"in-process concurrency {probe.peak} exceeded limit"
    assert db_peak <= 5, f"DB-observed running {db_peak} exceeded limit"
    assert probe.peak == 5, "limit should actually be reached (parallelism works)"
    assert queued_seen, "remaining jobs must wait in queue while 5 run"


def test_changing_limit_takes_effect_at_runtime_without_restart(db):
    store.set_app(db, "max_concurrency", "2")
    for _ in range(8):
        jobs.enqueue(db, "noop")
    probe = Probe(hold=0.6)
    w = jobs.Worker(runner=probe, poll=0.02, with_scheduler=False)
    w.start()
    try:
        assert wait_for(lambda: probe.peak == 2, 5)
        store.set_app(db, "max_concurrency", "4")
        assert wait_for(lambda: probe.peak == 4, 10)
        assert wait_for(lambda: count("completed") == 8, 30)
    finally:
        w.stop()
    assert probe.peak == 4


@pytest.mark.parametrize("limit", [10, 20])
def test_limit_ten_and_twenty_are_enforced(db, limit):
    if True:
        db.query(Job).delete()
        db.commit()
        store.set_app(db, "max_concurrency", str(limit))
        for _ in range(limit * 2):
            jobs.enqueue(db, "noop")
        probe = Probe(hold=0.4)
        w = jobs.Worker(runner=probe, poll=0.02, with_scheduler=False)
        w.start()
        try:
            assert wait_for(lambda: count("completed") == limit * 2, 60)
        finally:
            w.stop()
        assert probe.peak == limit


def test_claim_never_exceeds_cap_even_with_competing_claimers(db):
    """Many threads calling claim() at once must not over-claim (race test)."""
    store.set_app(db, "max_concurrency", "3")
    for _ in range(30):
        jobs.enqueue(db, "noop")
    claimed = []

    def go():
        s = dbmod.session()
        try:
            claimed.extend(j.id for j in jobs.claim(s))
        finally:
            s.close()

    ts = [threading.Thread(target=go) for _ in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(claimed) == 3 and len(set(claimed)) == 3
    assert count("running") == 3


def test_priority_then_fifo_order(db):
    store.set_app(db, "max_concurrency", "1")
    a, _ = jobs.enqueue(db, "noop", priority=1)
    time.sleep(0.01)
    b, _ = jobs.enqueue(db, "noop", priority=9)
    time.sleep(0.01)
    c, _ = jobs.enqueue(db, "noop", priority=9)
    first = jobs.claim(db)[0]
    assert first.id == b.id
    jobs.complete(db, b.id)
    assert jobs.claim(db)[0].id == c.id


def test_duplicate_prevention_active_only(db):
    j1, created1 = jobs.enqueue(db, "noop", dedupe_key="k1")
    j2, created2 = jobs.enqueue(db, "noop", dedupe_key="k1")
    assert created1 and not created2 and j1.id == j2.id
    store.set_app(db, "max_concurrency", "5")
    jobs.claim(db)
    jobs.complete(db, j1.id)
    j3, created3 = jobs.enqueue(db, "noop", dedupe_key="k1")  # allowed again once finished
    assert created3 and j3.id != j1.id


def test_retry_with_backoff_then_final_failure(db):
    j, _ = jobs.enqueue(db, "noop", max_attempts=2)
    jobs.claim(db)
    assert jobs.fail(db, j.id, "boom") == "queued"
    db.refresh(j)
    assert j.run_after > utcnow() + timedelta(seconds=3)       # backoff applied
    assert jobs.claim(db) == []                                # not runnable yet
    j.run_after = utcnow() - timedelta(seconds=1)
    db.commit()
    assert jobs.claim(db)[0].id == j.id
    assert jobs.fail(db, j.id, "boom again") == "failed"       # attempts exhausted
    db.refresh(j)
    assert j.status == "failed" and j.finished_at


def test_rate_limit_retry_after_is_honoured(db):
    j, _ = jobs.enqueue(db, "noop", max_attempts=3)
    jobs.claim(db)
    jobs.fail(db, j.id, "429", retry_after=300)
    db.refresh(j)
    assert j.run_after > utcnow() + timedelta(seconds=290)


def test_permanent_error_does_not_retry(db):
    def runner(task, payload, ctx):
        raise ValueError("bad input")
    j, _ = jobs.enqueue(db, "noop", max_attempts=5)
    w = jobs.Worker(runner=runner, poll=0.02, with_scheduler=False)
    w.start()
    try:
        assert wait_for(lambda: count("failed") == 1, 10)
    finally:
        w.stop()
    db.refresh(j)
    assert j.attempts == 1 and "bad input" in j.error


def test_transient_error_retried_by_worker(db, monkeypatch):
    monkeypatch.setattr(jobs, "backoff", lambda a, r=None: timedelta(milliseconds=50))
    calls = {"n": 0}

    def runner(task, payload, ctx):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RetryableError("temporary", None)
        return {"ok": True}
    j, _ = jobs.enqueue(db, "noop", max_attempts=4)
    w = jobs.Worker(runner=runner, poll=0.02, with_scheduler=False)
    w.start()
    try:
        assert wait_for(lambda: count("completed") == 1, 10)
    finally:
        w.stop()
    db.refresh(j)
    assert j.attempts == 3


def test_cancel_queued_and_running(db):
    q, _ = jobs.enqueue(db, "noop")
    assert jobs.cancel(db, q.id) == "cancelled"
    db.expire_all()
    from app import tasks
    j, _ = jobs.enqueue(db, "sleep", {"seconds": 30})
    w = jobs.Worker(runner=tasks.run_task, poll=0.02, with_scheduler=False)
    w.start()
    try:
        assert wait_for(lambda: count("running") == 1, 5)
        t0 = time.time()
        jobs.cancel(db, j.id)
        assert wait_for(lambda: count("cancelled") == 2, 5)
        assert time.time() - t0 < 4  # cooperative cancel, did not wait 30s
    finally:
        w.stop()


def test_retry_failed_and_cancelled(db):
    j, _ = jobs.enqueue(db, "noop", max_attempts=1)
    jobs.claim(db)
    jobs.fail(db, j.id, "x")
    jobs.retry(db, j.id)
    db.refresh(j)
    assert j.status == "queued" and j.attempts == 0


def test_interrupted_jobs_recovered_after_restart(db):
    j, _ = jobs.enqueue(db, "noop", max_attempts=3)
    jobs.claim(db)                          # now "running", but the process "dies"
    assert count("running") == 1
    w = jobs.Worker(runner=Probe(0.05), poll=0.02, with_scheduler=False)
    w.start()                               # startup recovery requeues it, then it runs
    try:
        assert wait_for(lambda: count("completed") == 1, 10)
    finally:
        w.stop()


def test_expired_lease_recovery_for_remote_jobs(db):
    j, _ = jobs.enqueue(db, "github_job", max_attempts=1)
    jobs.claim(db)
    j.lease_expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert jobs.recover_expired(db) == 1
    db.refresh(j)
    assert j.status == "failed" and "Interrupted" in j.error


def test_jobs_persist_across_engine_restart(db, dburl):
    j, _ = jobs.enqueue(db, "noop", {"keep": "me"})
    dbmod.engine.dispose()
    dbmod.configure(dburl)
    s = dbmod.session()
    try:
        got = s.get(Job, j.id)
        assert got and got.status == "queued" and '"keep"' in got.payload_json
    finally:
        s.close()


def test_api_cannot_set_invalid_concurrency(client):
    assert client.put("/api/settings/app", json={"max_concurrency": 0}).status_code == 400
    assert client.put("/api/settings/app", json={"max_concurrency": 101}).status_code == 400
    assert client.put("/api/settings/app", json={"max_concurrency": 10}).json()["max_concurrency"] == 10


def test_stats_come_from_real_records(client, db):
    assert client.get("/api/stats").json()["total"] == 0
    for _ in range(3):
        jobs.enqueue(db, "noop")
    jobs.claim(db)
    s = client.get("/api/stats").json()
    assert s["total"] == 3 and s["running"] == 3 and s["queued"] == 0
