import os, json, threading, secrets
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import select
from apscheduler.schedulers.background import BackgroundScheduler
from .db import Base, engine, get_db, SessionLocal
from .models import Setting, Job, Schedule
from .security import encrypt, decrypt, mask, require_admin, require_n8n_token, verify_callback
from .providers import test_llm
from .social import PLATFORMS
from .queue import worker_loop

scheduler=BackgroundScheduler(timezone="UTC")
thread=None

def enqueue(task,payload,max_attempts=3):
    db=SessionLocal()
    try:
        job=Job(task=task,payload_json=json.dumps(payload),status="queued",max_attempts=max_attempts)
        db.add(job); db.commit(); db.refresh(job); return job
    finally: db.close()

def fire_schedule(sid):
    db=SessionLocal()
    try:
        s=db.get(Schedule,sid)
        if s and s.enabled: enqueue(s.task,json.loads(s.payload_json))
    finally: db.close()

@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(bind=engine)
    global thread
    thread=threading.Thread(target=worker_loop,daemon=True); thread.start()
    db=SessionLocal()
    try:
        for s in db.scalars(select(Schedule).where(Schedule.enabled==True)).all():
            scheduler.add_job(fire_schedule,"interval",seconds=s.interval_seconds,args=[s.id],id=s.id,replace_existing=True)
    finally: db.close()
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)

app=FastAPI(title="n8n Automation Suite",version="1.0.0",lifespan=lifespan)
app.mount("/static",StaticFiles(directory="web"),name="static")

class LLMIn(BaseModel):
    name:str; provider:str; model:str; endpoint:str=""
    api_key:str
class SocialIn(BaseModel):
    name:str; platform:str; config:dict={}
    secrets:dict
class JobIn(BaseModel):
    task:str; payload:dict={}
    max_attempts:int=Field(default=3,ge=1,le=10)
class ScheduleIn(BaseModel):
    name:str; task:str; payload:dict={}
    interval_seconds:int=Field(ge=30,le=31536000)

@app.get("/")
def home(): return FileResponse("web/index.html")
@app.get("/health")
def health(): return {"ok":True,"service":"n8n-automation-suite","max_concurrent_jobs":int(os.getenv("MAX_CONCURRENT_JOBS","5"))}

@app.get("/api/settings/llm")
def list_llm(db:Session=Depends(get_db),_=Depends(require_admin)):
    rows=db.scalars(select(Setting).where(Setting.kind=="llm")).all()
    return [{"id":x.id,"name":x.name,**json.loads(x.config_json),"api_key_masked":mask(decrypt(x.secret_enc))} for x in rows]

@app.post("/api/settings/llm")
def save_llm(body:LLMIn,db:Session=Depends(get_db),_=Depends(require_admin)):
    if body.provider.lower() not in ["openai","gemini","anthropic","deepseek","openai-compatible","custom"]:
        raise HTTPException(400,"Unsupported LLM provider")
    x=Setting(kind="llm",name=body.name,config_json=json.dumps({"provider":body.provider,"model":body.model,"endpoint":body.endpoint}),secret_enc=encrypt(body.api_key))
    db.add(x); db.commit(); db.refresh(x)
    return {"id":x.id,"saved":True,"api_key_masked":mask(body.api_key)}

@app.post("/api/settings/llm/{sid}/test")
def llm_test(sid:str,db:Session=Depends(get_db),_=Depends(require_admin)):
    x=db.get(Setting,sid)
    if not x or x.kind!="llm": raise HTTPException(404,"Provider not found")
    c=json.loads(x.config_json)
    return test_llm(c["provider"],c["model"],c.get("endpoint",""),x.secret_enc)

@app.delete("/api/settings/llm/{sid}")
def delete_llm(sid:str,db:Session=Depends(get_db),_=Depends(require_admin)):
    x=db.get(Setting,sid)
    if not x or x.kind!="llm": raise HTTPException(404,"Provider not found")
    db.delete(x); db.commit(); return {"deleted":True}

@app.get("/api/settings/social")
def list_social(db:Session=Depends(get_db),_=Depends(require_admin)):
    rows=db.scalars(select(Setting).where(Setting.kind=="social")).all()
    return [{"id":x.id,"platform":x.name,"config":json.loads(x.config_json),"secret_configured":bool(x.secret_enc)} for x in rows]

@app.get("/api/settings/social/platforms")
def social_platforms(_=Depends(require_admin)): return PLATFORMS

@app.post("/api/settings/social")
def save_social(body:SocialIn,db:Session=Depends(get_db),_=Depends(require_admin)):
    p=body.platform.lower()
    if p not in PLATFORMS: raise HTTPException(400,"Unsupported social platform")
    missing=[f for f in PLATFORMS[p]["fields"] if not body.secrets.get(f)]
    if missing: raise HTTPException(400,"Missing required fields: "+", ".join(missing))
    x=Setting(kind="social",name=p,config_json=json.dumps(body.config),secret_enc=encrypt(json.dumps(body.secrets)))
    db.add(x); db.commit(); db.refresh(x); return {"id":x.id,"platform":p,"saved":True}

@app.delete("/api/settings/social/{sid}")
def delete_social(sid:str,db:Session=Depends(get_db),_=Depends(require_admin)):
    x=db.get(Setting,sid)
    if not x or x.kind!="social": raise HTTPException(404,"Account not found")
    db.delete(x); db.commit(); return {"deleted":True}

@app.post("/api/jobs")
def create_job(body:JobIn,db:Session=Depends(get_db),_=Depends(require_admin)):
    allowed={"noop","llm_prompt","social_publish","github_dispatch"}
    if body.task not in allowed: raise HTTPException(400,"Unsupported task")
    j=Job(task=body.task,payload_json=json.dumps(body.payload),status="queued",max_attempts=body.max_attempts)
    db.add(j); db.commit(); db.refresh(j); return {"id":j.id,"status":j.status}

@app.get("/api/jobs")
def list_jobs(limit:int=100,db:Session=Depends(get_db),_=Depends(require_admin)):
    rows=db.scalars(select(Job).order_by(Job.created_at.desc()).limit(min(limit,500))).all()
    return [{"id":j.id,"task":j.task,"status":j.status,"attempts":j.attempts,"error":j.error,"result":json.loads(j.result_json),"created_at":j.created_at.isoformat()} for j in rows]

@app.get("/api/jobs/{jid}")
def get_job(jid:str,db:Session=Depends(get_db),_=Depends(require_admin)):
    j=db.get(Job,jid)
    if not j: raise HTTPException(404,"Job not found")
    return {"id":j.id,"task":j.task,"status":j.status,"attempts":j.attempts,"error":j.error,"result":json.loads(j.result_json)}

@app.post("/api/jobs/{jid}/retry")
def retry_job(jid:str,db:Session=Depends(get_db),_=Depends(require_admin)):
    j=db.get(Job,jid)
    if not j: raise HTTPException(404,"Job not found")
    if j.status not in ("failed","cancelled"): raise HTTPException(409,"Only failed/cancelled jobs can be retried")
    j.status="queued"; j.error=""; j.attempts=0; db.commit(); return {"status":"queued"}

@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid:str,db:Session=Depends(get_db),_=Depends(require_admin)):
    j=db.get(Job,jid)
    if not j: raise HTTPException(404,"Job not found")
    if j.status=="running": raise HTTPException(409,"A running task cannot be forcibly stopped by this service")
    j.status="cancelled"; db.commit(); return {"status":"cancelled"}

@app.get("/api/schedules")
def list_schedules(db:Session=Depends(get_db),_=Depends(require_admin)):
    return [{"id":s.id,"name":s.name,"task":s.task,"interval_seconds":s.interval_seconds,"enabled":s.enabled,"payload":json.loads(s.payload_json)} for s in db.scalars(select(Schedule)).all()]

@app.post("/api/schedules")
def add_schedule(body:ScheduleIn,db:Session=Depends(get_db),_=Depends(require_admin)):
    if body.task not in {"noop","llm_prompt","social_publish","github_dispatch"}: raise HTTPException(400,"Unsupported task")
    s=Schedule(name=body.name,task=body.task,payload_json=json.dumps(body.payload),interval_seconds=body.interval_seconds,enabled=True)
    db.add(s); db.commit(); db.refresh(s)
    scheduler.add_job(fire_schedule,"interval",seconds=s.interval_seconds,args=[s.id],id=s.id,replace_existing=True)
    return {"id":s.id,"enabled":True}

@app.post("/api/schedules/{sid}/{action}")
def schedule_action(sid:str,action:str,db:Session=Depends(get_db),_=Depends(require_admin)):
    s=db.get(Schedule,sid)
    if not s: raise HTTPException(404,"Schedule not found")
    if action=="pause": s.enabled=False; scheduler.pause_job(sid) if scheduler.get_job(sid) else None
    elif action=="resume":
        s.enabled=True; scheduler.add_job(fire_schedule,"interval",seconds=s.interval_seconds,args=[sid],id=sid,replace_existing=True)
    else: raise HTTPException(400,"Action must be pause or resume")
    db.commit(); return {"enabled":s.enabled}

@app.delete("/api/schedules/{sid}")
def delete_schedule(sid:str,db:Session=Depends(get_db),_=Depends(require_admin)):
    s=db.get(Schedule,sid)
    if not s: raise HTTPException(404,"Schedule not found")
    if scheduler.get_job(sid): scheduler.remove_job(sid)
    db.delete(s); db.commit(); return {"deleted":True}

@app.post("/api/runner/callback")
def runner_callback(body:dict, x_callback_secret:str=Header(default="")):
    expected=os.getenv("CALLBACK_SECRET","")
    if not expected or not secrets.compare_digest(x_callback_secret,expected): raise HTTPException(401,"Invalid callback secret")
    db=SessionLocal()
    try:
        j=db.get(Job,body.get("job_id"))
        if not j: raise HTTPException(404,"Job not found")
        status=body.get("status")
        if status not in ("completed","failed"): raise HTTPException(400,"Invalid status")
        j.status=status; j.error=str(body.get("error",""))[:1000]; j.result_json=json.dumps(body.get("result",{})); db.commit()
        return {"ok":True}
    finally: db.close()

@app.post("/api/n8n/jobs",dependencies=[Depends(require_n8n_token)])
def n8n_create_job(body:JobIn,db:Session=Depends(get_db)):
    if body.task not in {"noop","llm_prompt","social_publish","github_dispatch"}: raise HTTPException(400,"Unsupported task")
    j=Job(task=body.task,payload_json=json.dumps(body.payload),status="queued",max_attempts=body.max_attempts)
    db.add(j); db.commit(); db.refresh(j); return {"id":j.id,"status":j.status}
