"""Task implementations. Credentialed tasks run on the control plane so secrets never leave the server."""
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import github_runner, llm, social, store
from .db import session


class Deferred:
    """Returned by tasks that finish asynchronously (completion arrives via signed callback)."""
    def __init__(self, timeout_seconds: int):
        self.timeout_seconds = timeout_seconds


@dataclass
class Ctx:
    job_id: str
    cancel_event: threading.Event = field(default_factory=threading.Event)
    log_fn: Callable[..., None] = lambda msg, level="info": None

    def log(self, msg: str, level: str = "info") -> None:
        self.log_fn(msg, level)

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()


def _noop(payload, ctx):
    return {"ok": True, "message": "No-op task completed"}


def _sleep(payload, ctx):
    end = time.monotonic() + float(payload.get("seconds", 1))
    while time.monotonic() < end:
        if ctx.cancelled():
            return {"cancelled": True}
        time.sleep(0.02)
    if payload.get("fail"):
        raise RuntimeError(str(payload["fail"]))
    return {"slept": payload.get("seconds", 1)}


def _llm_prompt(payload, ctx):
    db = session()
    try:
        p = store.llm_resolve(db, payload.get("provider_id"), payload.get("model"))
    finally:
        db.close()
    ctx.log(f"LLM request via {p['provider']}/{p['model']}")
    text = llm.generate(p["provider"], p["model"], p["endpoint"], p["key"], payload["prompt"],
                        payload.get("system", ""), int(payload.get("max_tokens", 1024)))
    return {"text": text, "provider": p["provider"], "model": p["model"]}


def _publish(payload: dict, ctx: Ctx) -> dict:
    db = session()
    try:
        platform, creds, cfg = store.social_resolve(db, payload["account_id"])
    finally:
        db.close()
    module = social.get(platform, creds, cfg)
    missing = module.validate(creds)
    if missing:
        raise ValueError("Missing credential fields: " + ", ".join(missing))
    data = dict(payload)
    if not payload.get("raw"):
        data.update(social.format_text(platform, payload.get("text", ""), payload.get("title", "")))
    ctx.log(f"Publishing to {platform}")
    res = module.publish(data)
    ctx.log(f"Published to {platform}: {res.get('id')}")
    return {"status": "published", "platform": platform, **res}


def _ai_publish(payload, ctx):
    db = session()
    try:
        platform, _, _ = store.social_resolve(db, payload["account_id"])
        p = store.llm_resolve(db, payload.get("provider_id"), payload.get("model"))
    finally:
        db.close()
    system = (f"You write social media posts for {platform}. Output ONLY the post text, "
              f"max {social.LIMITS.get(platform, 2000)} characters, no preamble.")
    ctx.log(f"Generating content via {p['provider']}/{p['model']}")
    text = llm.generate(p["provider"], p["model"], p["endpoint"], p["key"], payload["prompt"], system, 800)
    result = _publish({**payload, "text": text}, ctx)
    result["generated_text"] = text
    return result


def _github_job(payload, ctx):
    db = session()
    try:
        cfg = store.github_config(db)
    finally:
        db.close()
    github_runner.dispatch(cfg, ctx.job_id)
    ctx.log("Dispatched to GitHub Actions; waiting for signed callback")
    from .config import env_int
    return Deferred(env_int("GITHUB_JOB_TIMEOUT_SECONDS", 1800))


TASKS = {"noop": _noop, "sleep": _sleep, "llm_prompt": _llm_prompt, "social_publish": _publish,
         "ai_publish": _ai_publish, "github_job": _github_job}
USER_TASKS = ("noop", "llm_prompt", "social_publish", "ai_publish", "github_job", "sleep")


def run_task(task: str, payload: dict, ctx: Ctx):
    fn = TASKS.get(task)
    if not fn:
        raise ValueError(f"Unknown task: {task}")
    return fn(payload, ctx)
