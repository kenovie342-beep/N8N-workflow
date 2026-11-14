import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from . import __version__, db as dbmod, jobs, llm, logsafe, ratelimit, scheduler, social, store, tasks
from .config import env, is_production, validate_production
from .http import PermanentError, RetryableError
from .http import request as http_request
from .models import Job, JobLog, Schedule, Setting, iso, utcnow
from .security import (decrypt_json, encrypt, encrypt_json, make_session, mask, mask_map, read_session,
                       register_nonce, require_admin, require_n8n_token, verify_login, verify_signature)

log = logging.getLogger("suite")
WEB = Path(__file__).resolve().parent.parent / "web"
worker: jobs.Worker | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"))
    logsafe.install()
    if is_production():
        problems = validate_production()
        if problems:
            raise RuntimeError("Refusing to start in production: " + "; ".join(problems))
    dbmod.configure()
    if env("APP_AUTO_CREATE_TABLES") == "1":  # dev/test only; production uses `alembic upgrade head`
        from .db import Base
        from . import models  # noqa: F401
        assert dbmod.engine is not None
        Base.metadata.create_all(bind=dbmod.engine)
    if env("APP_DISABLE_WORKER") != "1":
        worker = jobs.Worker(poll=float(env("WORKER_POLL_SECONDS", "1")))
        worker.start()
    yield
    if worker:
        worker.stop()


app = FastAPI(title="n8n Automation Suite", version=__version__, lifespan=lifespan, docs_url="/docs" if not is_production() else None,
              redoc_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"
    if request.url.path.startswith("/api"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def rl(request: Request):
    ratelimit.limit(request, "api", 600)


# ------------------------------------------------------------------ schemas
def _url_ok(v: str) -> str:
    if v and not (v.startswith("http://") or v.startswith("https://")):
        raise ValueError("endpoint must start with http:// or https://")
    return v


class LLMIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    provider: str
    model: str = Field(min_length=1, max_length=120)
    endpoint: str = ""
    api_key: str = ""
    make_default: bool = False
    _v = field_validator("endpoint")(_url_ok)

    @field_validator("provider")
    @classmethod
    def _prov(cls, v):
        if v.lower() not in llm.PROVIDERS:
            raise ValueError("Unsupported provider. Use one of: " + ", ".join(llm.PROVIDERS))
        return v.lower()


class LLMUpdate(BaseModel):
    name: str | None = None
    provider: str | None = None
    model: str | None = None
    endpoint: str | None = None
    api_key: str | None = None


class SocialIn(BaseModel):
    name: str = Field(default="", max_length=80)
    platform: str
    config: dict = {}
    secrets: dict = {}


class JobIn(BaseModel):
    task: str
    payload: dict = {}
    workflow: str = ""
    priority: int = Field(default=5, ge=0, le=10)
    max_attempts: int = Field(default=3, ge=1, le=10)
    dedupe_key: str | None = Field(default=None, max_length=200)
    run_after: str | None = None


class ScheduleIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    task: str
    workflow: str = ""
    payload: dict = {}
    kind: str = "cron"
    cron_expr: str = ""
    run_at: str | None = None
    interval_seconds: int = 0
    timezone: str | None = None
    priority: int = Field(default=5, ge=0, le=10)
    enabled: bool = True


class SchedulePatch(BaseModel):
    name: str | None = None
    task: str | None = None
    workflow: str | None = None
    payload: dict | None = None
    kind: str | None = None
    cron_expr: str | None = None
    run_at: str | None = None
    interval_seconds: int | None = None
    timezone: str | None = None
    priority: int | None = None


# ---------------------------------------------------------------- public
@app.get("/health")
def health():
    try:
        s = dbmod.session()
        try:
            s.execute(text("SELECT 1"))
        finally:
            s.close()
    except Exception:
        return JSONResponse({"ok": False, "db": False}, status_code=503)
    return {"ok": True, "db": True, "version": __version__, "worker": bool(worker and worker._thread and worker._thread.is_alive())}


@app.get("/")
def home():
    return FileResponse(WEB / "index.html")


# ------------------------------------------------------------------- auth
@app.post("/api/auth/login")
def login(request: Request, response: Response, body: dict = Body(...)):
    ratelimit.limit(request, "login", 10)
    if not verify_login(str(body.get("username", "")), str(body.get("password", ""))):
        raise HTTPException(401, "Invalid credentials")
    token, csrf = make_session(str(body["username"]))
    response.set_cookie("suite_session", token, httponly=True, samesite="strict",
                        secure=env("PUBLIC_BASE_URL").startswith("https"), max_age=12 * 3600, path="/")
    return {"ok": True, "csrf": csrf}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie("suite_session", path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def me(request: Request):
    s = read_session(request.cookies.get("suite_session"))
    if not s:
        raise HTTPException(401, "Authentication required")
    return {"user": s["u"], "csrf": s["csrf"]}


# ----------------------------------------------------------- app settings
@app.get("/api/settings/app")
def get_app_settings(db: Session = Depends(dbmod.get_db), _=Depends(require_admin), __=Depends(rl)):
    return {"max_concurrency": store.get_max_concurrency(db), "default_timezone": store.get_timezone(db)}


@app.put("/api/settings/app")
def put_app_settings(body: dict = Body(...), db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    if "max_concurrency" in body:
        try:
            n = int(body["max_concurrency"])
        except (TypeError, ValueError):
            raise HTTPException(400, "max_concurrency must be an integer")
        if not 1 <= n <= 100:
            raise HTTPException(400, "max_concurrency must be between 1 and 100")
        store.set_app(db, "max_concurrency", str(n))
    if "default_timezone" in body:
        try:
            scheduler._tz(str(body["default_timezone"]))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        store.set_app(db, "default_timezone", str(body["default_timezone"]))
    return get_app_settings(db)


# --------------------------------------------------------------------- LLM
def _llm_view(x: Setting) -> dict:
    c = json.loads(x.config_json)
    return {"id": x.id, "name": x.name, "provider": c["provider"], "model": c["model"], "endpoint": c.get("endpoint", ""),
            "api_key_masked": c.get("key_mask", ""), "is_default": x.is_default}


@app.get("/api/settings/llm")
def list_llm(db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    return [_llm_view(x) for x in db.scalars(select(Setting).where(Setting.kind == "llm").order_by(Setting.created_at))]


@app.get("/api/settings/llm/providers")
def llm_provider_types(_=Depends(require_admin)):
    return {"providers": llm.PROVIDERS, "default_endpoints": llm.DEFAULT_ENDPOINTS}


@app.post("/api/settings/llm")
def save_llm(body: LLMIn, db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    if body.provider in ("openai-compatible", "custom", "local") and not body.endpoint:
        raise HTTPException(400, "An endpoint is required for custom / OpenAI-compatible / local providers")
    has_any = (db.scalar(select(func.count()).select_from(Setting).where(Setting.kind == "llm")) or 0) > 0
    x = Setting(kind="llm", name=body.name, secret_enc=encrypt(body.api_key) if body.api_key else "",
                config_json=json.dumps({"provider": body.provider, "model": body.model, "endpoint": body.endpoint,
                                        "key_mask": mask(body.api_key)}),
                is_default=body.make_default or not has_any)
    if x.is_default:
        for o in db.scalars(select(Setting).where(Setting.kind == "llm", Setting.is_default == True)):  # noqa: E712
            o.is_default = False
    db.add(x)
    db.commit()
    logsafe.register_secret(body.api_key)
    return _llm_view(x)


@app.put("/api/settings/llm/{sid}")
def update_llm(sid: str, body: LLMUpdate, db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    x = db.get(Setting, sid)
    if not x or x.kind != "llm":
        raise HTTPException(404, "Provider not found")
    c = json.loads(x.config_json)
    if body.provider:
        if body.provider.lower() not in llm.PROVIDERS:
            raise HTTPException(400, "Unsupported provider")
        c["provider"] = body.provider.lower()
    if body.model:
        c["model"] = body.model
    if body.endpoint is not None:
        try:
            _url_ok(body.endpoint)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        c["endpoint"] = body.endpoint
    if body.name:
        x.name = body.name
    if body.api_key:  # blank = keep the stored key
        x.secret_enc, c["key_mask"] = encrypt(body.api_key), mask(body.api_key)
    x.config_json, x.updated_at = json.dumps(c), utcnow()
    db.commit()
    return _llm_view(x)


@app.post("/api/settings/llm/{sid}/default")
def default_llm(sid: str, db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    x = db.get(Setting, sid)
    if not x or x.kind != "llm":
        raise HTTPException(404, "Provider not found")
    for o in db.scalars(select(Setting).where(Setting.kind == "llm")):
        o.is_default = o.id == sid
    db.commit()
    return {"default": sid}


@app.post("/api/settings/llm/{sid}/test")
def test_llm(sid: str, request: Request, db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    ratelimit.limit(request, "test", 20)
    try:
        p = store.llm_resolve(db, sid)
    except LookupError:
        raise HTTPException(404, "Provider not found")
    return llm.test_connection(p["provider"], p["model"], p["endpoint"], p["key"])


@app.delete("/api/settings/llm/{sid}")
def delete_llm(sid: str, db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    x = db.get(Setting, sid)
    if not x or x.kind != "llm":
        raise HTTPException(404, "Provider not found")
    db.delete(x)
    db.commit()
    return {"deleted": True}


# ------------------------------------------------------------------- social
def _social_view(x: Setting) -> dict:
    c = json.loads(x.config_json)
    return {"id": x.id, "platform": x.name, "label": c.get("label", ""), "config": c.get("config", {}),
            "secrets_masked": c.get("masked", {}), "configured_fields": sorted(c.get("masked", {}))}


@app.get("/api/settings/social/platforms")
def social_platforms(_=Depends(require_admin)):
    return social.spec()


@app.get("/api/settings/social")
def list_social(db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    return [_social_view(x) for x in db.scalars(select(Setting).where(Setting.kind == "social").order_by(Setting.created_at))]


def _social_missing(platform: str, secrets_map: dict) -> list[str]:
    return social.REGISTRY[platform].validate(secrets_map)


@app.post("/api/settings/social")
def save_social(body: SocialIn, db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    p = body.platform.lower()
    if p not in social.REGISTRY:
        raise HTTPException(400, "Unsupported social platform")
    missing = _social_missing(p, body.secrets)
    if missing:
        raise HTTPException(400, "Missing required fields: " + ", ".join(missing))
    x = Setting(kind="social", name=p, secret_enc=encrypt_json(body.secrets),
                config_json=json.dumps({"label": body.name, "config": body.config, "masked": mask_map(body.secrets)}))
    db.add(x)
    db.commit()
    for v in body.secrets.values():
        logsafe.register_secret(str(v))
    return _social_view(x)


@app.put("/api/settings/social/{sid}")
def update_social(sid: str, body: SocialIn, db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    x = db.get(Setting, sid)
    if not x or x.kind != "social":
        raise HTTPException(404, "Account not found")
    merged = decrypt_json(x.secret_enc)
    merged.update({k: v for k, v in body.secrets.items() if v})  # blank = keep stored value
    missing = _social_missing(x.name, merged)
    if missing:
        raise HTTPException(400, "Missing required fields: " + ", ".join(missing))
    x.secret_enc = encrypt_json(merged)
    x.config_json = json.dumps({"label": body.name, "config": body.config, "masked": mask_map(merged)})
    x.updated_at = utcnow()
    db.commit()
    return _social_view(x)


@app.post("/api/settings/social/{sid}/test")
def test_social(sid: str, request: Request, db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    ratelimit.limit(request, "test", 20)
    try:
        platform, creds, cfg = store.social_resolve(db, sid)
    except LookupError:
        raise HTTPException(404, "Account not found")
    try:
        return social.get(platform, creds, cfg).test_connection()
    except (RetryableError, PermanentError) as exc:
        return {"ok": False, "message": str(exc)[:400]}
    except Exception as exc:  # malformed responses etc.
        return {"ok": False, "message": f"{type(exc).__name__}: {str(exc)[:300]}"}


@app.delete("/api/settings/social/{sid}")
def delete_social(sid: str, db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    x = db.get(Setting, sid)
    if not x or x.kind != "social":
        raise HTTPException(404, "Account not found")
    db.delete(x)
    db.commit()
    return {"deleted": True}


# ------------------------------------------------------- github / cloudflare
def _singleton_view(db, kind: str, secret_keys: tuple) -> dict:
    s = db.scalars(select(Setting).where(Setting.kind == kind)).first()
    if not s:
        return {"configured": False}
    cfg = json.loads(s.config_json)
    sec = decrypt_json(s.secret_enc)
    return {"configured": True, **cfg, **{f"{k}_masked": mask(sec.get(k, "")) for k in secret_keys}}


@app.get("/api/settings/github")
def get_github(db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    return _singleton_view(db, "github", ("token",))


@app.put("/api/settings/github")
def put_github(body: dict = Body(...), db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    cfg = {k: str(body.get(k, "")).strip() for k in ("owner", "repo", "workflow", "ref")}
    store.upsert_singleton(db, "github", cfg, {"token": str(body.get("token", ""))})
    return _singleton_view(db, "github", ("token",))


@app.post("/api/settings/github/test")
def test_github(db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    cfg = store.github_config(db)
    if not cfg.get("token"):
        return {"ok": False, "message": "GitHub is not configured"}
    try:
        r = http_request("GET", f"https://api.github.com/repos/{cfg['owner']}/{cfg['repo']}/actions/workflows/{cfg['workflow']}",
                         headers={"Authorization": f"Bearer {cfg['token']}", "Accept": "application/vnd.github+json"},
                         secrets=(cfg["token"],))
        return {"ok": True, "workflow": r.json().get("name"), "state": r.json().get("state")}
    except (RetryableError, PermanentError) as exc:
        return {"ok": False, "message": str(exc)[:400]}


@app.get("/api/settings/cloudflare")
def get_cloudflare(db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    return _singleton_view(db, "cloudflare", ("api_token", "shared_secret"))


@app.put("/api/settings/cloudflare")
def put_cloudflare(body: dict = Body(...), db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    cfg = {k: str(body.get(k, "")).strip() for k in ("account_id", "worker_url", "d1_database_id", "r2_bucket")}
    if cfg["worker_url"]:
        _url_ok(cfg["worker_url"])
    store.upsert_singleton(db, "cloudflare", cfg, {"api_token": str(body.get("api_token", "")),
                                                   "shared_secret": str(body.get("shared_secret", ""))})
    return _singleton_view(db, "cloudflare", ("api_token", "shared_secret"))


@app.post("/api/settings/cloudflare/test")
def test_cloudflare(db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    cfg = store.cloudflare_config(db)
    out: dict = {"ok": True, "checks": {}}
    try:
        if not cfg.get("api_token"):
            raise PermanentError("API token is not configured")
        base = "https://api.cloudflare.com/client/v4"
        url = f"{base}/accounts/{cfg['account_id']}/tokens/verify" if cfg.get("account_id") else f"{base}/user/tokens/verify"
        r = http_request("GET", url, headers={"Authorization": f"Bearer {cfg['api_token']}"}, secrets=(cfg["api_token"],))
        out["checks"]["api_token"] = r.json().get("result", {}).get("status", "unknown")
    except (RetryableError, PermanentError) as exc:
        out["ok"], out["checks"]["api_token"] = False, str(exc)[:300]
    if cfg.get("worker_url"):
        try:
            r = http_request("GET", cfg["worker_url"].rstrip("/") + "/health")
            out["checks"]["worker"] = "ok" if r.status_code == 200 else r.status_code
        except (RetryableError, PermanentError) as exc:
            out["ok"], out["checks"]["worker"] = False, str(exc)[:300]
    return out


# --------------------------------------------------------------------- jobs
def _validate_job(db, task: str, payload: dict) -> None:
    if task not in tasks.USER_TASKS:
        raise HTTPException(400, f"Unsupported task. Allowed: {', '.join(tasks.USER_TASKS)}")
    if len(json.dumps(payload)) > 64_000:
        raise HTTPException(413, "payload too large")
    if task in ("social_publish", "ai_publish"):
        a = db.get(Setting, str(payload.get("account_id", "")))
        if not a or a.kind != "social":
            raise HTTPException(400, "payload.account_id must reference a saved social account")
    if task == "llm_prompt" and not payload.get("prompt"):
        raise HTTPException(400, "payload.prompt is required")


def _create_job(db, body: JobIn):
    _validate_job(db, body.task, body.payload)
    run_after = None
    if body.run_after:
        from datetime import datetime
        try:
            run_after = scheduler.to_utc(datetime.fromisoformat(body.run_after.replace("Z", "+00:00")), store.get_timezone(db))
        except ValueError:
            raise HTTPException(400, "run_after must be an ISO-8601 datetime")
    j, created = jobs.enqueue(db, body.task, body.payload, priority=body.priority, max_attempts=body.max_attempts,
                              dedupe_key=body.dedupe_key, run_after=run_after, workflow=body.workflow)
    return {"id": j.id, "status": j.status, "created": created}


@app.post("/api/jobs")
def create_job(body: JobIn, db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    return _create_job(db, body)


@app.get("/api/jobs")
def list_jobs(status: str | None = None, task: str | None = None, limit: int = Query(100, le=500), offset: int = 0,
              db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    q = select(Job).order_by(Job.created_at.desc()).limit(limit).offset(offset)
    if status:
        q = q.where(Job.status == status)
    if task:
        q = q.where(Job.task == task)
    return [jobs.job_dict(j) for j in db.scalars(q)]


@app.get("/api/jobs/{jid}")
def get_job(jid: str, db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    j = db.get(Job, jid)
    if not j:
        raise HTTPException(404, "Job not found")
    d = jobs.job_dict(j, full=True)
    d["logs"] = [{"ts": iso(lg.ts), "level": lg.level, "message": lg.message}
                 for lg in db.scalars(select(JobLog).where(JobLog.job_id == jid).order_by(JobLog.id))]
    return d


@app.get("/api/jobs/{jid}/remote-logs")
def remote_logs(jid: str, db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    from . import github_runner
    j = db.get(Job, jid)
    if not j or j.executor != "github":
        raise HTTPException(404, "No GitHub run for this job")
    cfg = store.github_config(db)
    try:
        run_id = j.remote_run_id
        if not run_id:
            run = github_runner.find_run(cfg, jid)
            if not run:
                raise HTTPException(404, "Run not found yet")
            run_id = str(run["id"])
            j.remote_run_id = run_id
            db.commit()
        return {"run_id": run_id, "logs": github_runner.get_logs(cfg, run_id)}
    except (RetryableError, PermanentError) as exc:
        raise HTTPException(502, str(exc)[:300])


@app.post("/api/jobs/{jid}/retry")
def retry_job(jid: str, db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    try:
        jobs.retry(db, jid)
    except LookupError:
        raise HTTPException(404, "Job not found")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"status": "queued"}


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str, db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    try:
        return {"status": jobs.cancel(db, jid)}
    except LookupError:
        raise HTTPException(404, "Job not found")


@app.get("/api/stats")
def get_stats(db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    return jobs.stats(db)


# ---------------------------------------------------------------- schedules
def _sched_view(s: Schedule) -> dict:
    return {"id": s.id, "name": s.name, "task": s.task, "workflow": s.workflow, "payload": json.loads(s.payload_json),
            "kind": s.kind, "cron_expr": s.cron_expr, "run_at": iso(s.run_at), "interval_seconds": s.interval_seconds,
            "timezone": s.timezone, "priority": s.priority, "enabled": s.enabled, "next_run_at": iso(s.next_run_at),
            "last_run_at": iso(s.last_run_at)}


@app.get("/api/schedules")
def list_schedules(db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    return [_sched_view(s) for s in db.scalars(select(Schedule).order_by(Schedule.created_at))]


@app.get("/api/schedules/upcoming")
def upcoming(limit: int = 20, db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    return scheduler.upcoming(db, limit=min(limit, 100))


@app.post("/api/schedules")
def add_schedule(body: ScheduleIn, db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    _validate_job(db, body.task, body.payload)
    s = Schedule(name=body.name, task=body.task, enabled=body.enabled)
    try:
        scheduler.apply_fields(s, body.model_dump(exclude={"enabled"}), store.get_timezone(db))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    db.add(s)
    db.commit()
    return _sched_view(s)


@app.put("/api/schedules/{sid}")
def edit_schedule(sid: str, body: SchedulePatch, db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    s = db.get(Schedule, sid)
    if not s:
        raise HTTPException(404, "Schedule not found")
    data = body.model_dump(exclude_none=True)
    if "task" in data or "payload" in data:
        _validate_job(db, data.get("task", s.task), data.get("payload", json.loads(s.payload_json)))
    try:
        scheduler.apply_fields(s, data, store.get_timezone(db))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))
    db.commit()
    return _sched_view(s)


def _set_enabled(db, sid: str, enabled: bool) -> dict:
    s = db.get(Schedule, sid)
    if not s:
        raise HTTPException(404, "Schedule not found")
    s.enabled = enabled
    s.next_run_at = scheduler.compute_next(s.kind, cron_expr=s.cron_expr, run_at=s.run_at,
                                           interval_seconds=s.interval_seconds, tzname=s.timezone) if enabled else None
    s.updated_at = utcnow()
    db.commit()
    return _sched_view(s)


@app.post("/api/schedules/{sid}/pause")
def pause_schedule(sid: str, db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    return _set_enabled(db, sid, False)


@app.post("/api/schedules/{sid}/resume")
def resume_schedule(sid: str, db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    return _set_enabled(db, sid, True)


@app.post("/api/schedules/{sid}/run-now")
def run_schedule_now(sid: str, db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    s = db.get(Schedule, sid)
    if not s:
        raise HTTPException(404, "Schedule not found")
    j, _c = jobs.enqueue(db, s.task, json.loads(s.payload_json), priority=s.priority, schedule_id=s.id, workflow=s.workflow or s.name)
    return {"id": j.id}


@app.get("/api/schedules/{sid}/runs")
def schedule_runs(sid: str, limit: int = 50, db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    return [jobs.job_dict(j) for j in db.scalars(select(Job).where(Job.schedule_id == sid).order_by(Job.created_at.desc()).limit(min(limit, 200)))]


@app.delete("/api/schedules/{sid}")
def delete_schedule(sid: str, db: Session = Depends(dbmod.get_db), _=Depends(require_admin)):
    s = db.get(Schedule, sid)
    if not s:
        raise HTTPException(404, "Schedule not found")
    db.delete(s)
    db.commit()
    return {"deleted": True}


# ------------------------------------------- signed endpoints (runner / CF)
async def _signed(request: Request, db: Session, job_id_hint: str = "") -> tuple[str, str]:
    ratelimit.limit(request, "signed", 300)
    body = (await request.body()).decode()
    nonce, _ts = verify_signature(env("CALLBACK_SECRET"), request.headers, body)
    return body, nonce


@app.post("/api/runner/callback")
async def runner_callback(request: Request, db: Session = Depends(dbmod.get_db)):
    body, nonce = await _signed(request, db)
    try:
        data = json.loads(body)
        job_id, status = str(data["job_id"]), data["status"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "Invalid callback body")
    if status not in ("completed", "failed"):
        raise HTTPException(400, "status must be completed or failed")
    if not register_nonce(db, nonce, job_id):
        return {"ok": True, "duplicate": True, "applied": False}  # replayed request: no state change
    try:
        job = db.get(Job, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        for line in str(data.get("logs", ""))[:20000].splitlines()[:200]:
            jobs.add_log(db, job_id, line)
        if status == "completed":
            applied = jobs.complete(db, job_id, data.get("result") or {})
        else:
            applied = jobs.fail(db, job_id, str(data.get("error", "Runner reported failure")),
                                retryable=bool(data.get("retryable", False))) is not None
    except HTTPException:
        raise
    except Exception:
        from .models import CallbackNonce
        db.rollback()
        db.query(CallbackNonce).filter(CallbackNonce.nonce == nonce).delete()
        db.commit()
        raise
    return {"ok": True, "duplicate": not applied, "applied": applied}


@app.get("/api/runner/jobs/{job_id}/payload")
async def runner_payload(job_id: str, request: Request, db: Session = Depends(dbmod.get_db)):
    ratelimit.limit(request, "signed", 300)
    nonce, _ = verify_signature(env("CALLBACK_SECRET"), request.headers, job_id)
    if not register_nonce(db, nonce, job_id):
        raise HTTPException(409, "Replayed request")
    j = db.get(Job, job_id)
    if not j or j.executor != "github" or j.status != "running":
        raise HTTPException(404, "No running remote job with that id")
    return {"job_id": job_id, "payload": json.loads(j.payload_json)}


@app.post("/api/cf/tick")
async def cf_tick(request: Request, db: Session = Depends(dbmod.get_db)):
    body, nonce = await _signed(request, db)
    if not register_nonce(db, nonce, "cf-tick"):
        return {"ok": True, "duplicate": True}
    created = scheduler.tick(db)
    return {"ok": True, "scheduled_jobs_created": created, "stats": {k: v for k, v in jobs.stats(db).items() if k != "recent"}}


# --------------------------------------------------------------------- n8n
@app.post("/api/n8n/jobs", dependencies=[Depends(require_n8n_token)])
def n8n_create_job(body: JobIn, db: Session = Depends(dbmod.get_db)):
    return _create_job(db, body)


@app.get("/api/n8n/jobs/{jid}", dependencies=[Depends(require_n8n_token)])
def n8n_get_job(jid: str, db: Session = Depends(dbmod.get_db)):
    j = db.get(Job, jid)
    if not j:
        raise HTTPException(404, "Job not found")
    return jobs.job_dict(j, full=True)


@app.post("/api/n8n/generate", dependencies=[Depends(require_n8n_token)])
def n8n_generate(body: dict = Body(...), db: Session = Depends(dbmod.get_db)):
    """AI content generation for n8n workflows, using a saved provider (default if none given)."""
    prompt = str(body.get("prompt", "")).strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")
    try:
        p = store.llm_resolve(db, body.get("provider_id") or None, body.get("model") or None)
        text_out = llm.generate(p["provider"], p["model"], p["endpoint"], p["key"], prompt, str(body.get("system", "")),
                                int(body.get("max_tokens", 800)))
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except (RetryableError, PermanentError) as exc:
        raise HTTPException(502, str(exc)[:300])
    return {"text": text_out, "provider": p["provider"], "model": p["model"]}


@app.get("/api/n8n/accounts", dependencies=[Depends(require_n8n_token)])
def n8n_accounts(db: Session = Depends(dbmod.get_db)):
    return {"social": [{"id": x.id, "platform": x.name, "label": json.loads(x.config_json).get("label", "")}
                       for x in db.scalars(select(Setting).where(Setting.kind == "social"))],
            "llm": [{"id": x.id, "name": x.name, "default": x.is_default} for x in db.scalars(select(Setting).where(Setting.kind == "llm"))]}


@app.exception_handler(RuntimeError)
async def runtime_error(request: Request, exc: RuntimeError):
    log.error("runtime error: %s", exc)
    return JSONResponse({"detail": "Server configuration error (see server logs)"}, status_code=500)
