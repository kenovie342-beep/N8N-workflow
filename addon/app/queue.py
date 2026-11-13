import os, json, threading, time, requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from sqlalchemy import select
from .db import SessionLocal
from .models import Job
from .security import decrypt

_lock = threading.Lock()
def utcnow(): return datetime.now(timezone.utc)

def dispatch_github(job: Job, payload: dict):
    token=os.getenv("GITHUB_TOKEN",""); owner=os.getenv("GITHUB_OWNER",""); repo=os.getenv("GITHUB_REPO","")
    workflow=os.getenv("GITHUB_WORKFLOW_ID","remote-job.yml"); ref=os.getenv("GITHUB_REF","main")
    if not all([token,owner,repo]): raise RuntimeError("GitHub Actions integration is not configured")
    r=requests.post(f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches",
       headers={"Authorization":f"Bearer {token}","Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"},
       json={"ref":ref,"inputs":{"job_id":job.id,"task":job.task,"payload_json":json.dumps(payload)}},timeout=20)
    if r.status_code != 204: raise RuntimeError(f"GitHub dispatch failed: {r.status_code} {r.text[:300]}")

def run_local(job: Job, payload: dict):
    if job.task == "noop": return {"ok":True,"message":"No-op task completed"}
    if job.task == "social_publish":
        from .social import publish
        from .models import Setting
        db=SessionLocal()
        try:
            setting=db.get(Setting,payload["account_id"])
            if not setting or setting.kind!="social": raise RuntimeError("Social account not found")
            return publish(setting.name, json.loads(setting.config_json), setting.secret_enc, payload)
        finally: db.close()
    if job.task == "llm_prompt":
        from .models import Setting
        from .providers import test_llm
        db=SessionLocal()
        try:
            setting=db.get(Setting,payload["provider_id"])
            if not setting or setting.kind!="llm": raise RuntimeError("LLM provider not found")
            cfg=json.loads(setting.config_json)
            # LLM task uses OpenAI-compatible endpoint for text generation.
            key=decrypt(setting.secret_enc)
            endpoint=(cfg.get("endpoint") or "https://api.openai.com/v1").rstrip("/")+"/chat/completions"
            r=requests.post(endpoint,headers={"Authorization":"Bearer "+key},
              json={"model":cfg["model"],"messages":[{"role":"user","content":payload["prompt"]}]},timeout=90)
            r.raise_for_status()
            return {"text":r.json()["choices"][0]["message"]["content"]}
        finally: db.close()
    if job.task == "github_dispatch":
        dispatch_github(job,payload); return {"accepted_by_github":True}
    raise RuntimeError(f"Unknown task: {job.task}")


_executor = None
def _execute_job(jid):
    db=SessionLocal()
    job=None
    try:
        job=db.get(Job,jid)
        if not job: return
        payload=json.loads(job.payload_json)
        result=run_local(job,payload)
        job.status="completed"; job.result_json=json.dumps(result); job.error=""
    except Exception as exc:
        if job:
            job.error=str(exc)[:1500]
            job.status="queued" if job.attempts < job.max_attempts else "failed"
    finally:
        if job:
            job.updated_at=utcnow(); db.commit()
        db.close()

def pump_once():
    global _executor
    max_jobs=max(1,int(os.getenv("MAX_CONCURRENT_JOBS","5")))
    if _executor is None:
        _executor=ThreadPoolExecutor(max_workers=max_jobs, thread_name_prefix="suite-job")
    with _lock:
        db=SessionLocal()
        try:
            running=len(db.scalars(select(Job).where(Job.status=="running")).all())
            capacity=max(0,max_jobs-running)
            queued=db.scalars(select(Job).where(Job.status=="queued").order_by(Job.created_at).limit(capacity)).all()
            ids=[]
            for job in queued:
                job.status="running"; job.attempts+=1; job.updated_at=utcnow(); ids.append(job.id)
            db.commit()
        finally: db.close()
    for jid in ids:
        _executor.submit(_execute_job,jid)

def worker_loop():
    while True:
        try: pump_once()
        except Exception: pass
        time.sleep(max(1,int(os.getenv("SCHEDULER_POLL_SECONDS","10"))))
