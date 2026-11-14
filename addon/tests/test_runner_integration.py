"""Runs the REAL runner script against a REAL local uvicorn server (GitHub itself is not involved)."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import requests
import uvicorn

from app import db as dbmod, jobs
from app.models import Job

RUNNER = Path(__file__).resolve().parent.parent / "github-actions" / "suite_runner.py"


@pytest.fixture
def live(dburl):
    from app.main import app
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True); t.start()
    for _ in range(100):
        try:
            if requests.get(f"http://127.0.0.1:{port}/health", timeout=1).ok:
                break
        except requests.RequestException:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(timeout=5)


def run_runner(base, job_id, secret="cb-secret-for-tests-0123456789"):
    env = {**os.environ, "JOB_ID": job_id, "SUITE_URL": base, "CALLBACK_SECRET": secret, "CALLBACK_MAX_ATTEMPTS": "1"}
    return subprocess.run([sys.executable, str(RUNNER)], env=env, capture_output=True, text=True, timeout=60)


def make_remote(payload, max_attempts=2):
    s = dbmod.session()
    try:
        j, _ = jobs.enqueue(s, "github_job", payload, max_attempts=max_attempts)
        jobs.claim(s)
        return j.id
    finally:
        s.close()


def fetch(jid):
    s = dbmod.session()
    try:
        j = s.get(Job, jid)
        s.refresh(j)
        return j
    finally:
        s.close()


def test_runner_end_to_end_success(live):
    jid = make_remote({"task": "echo", "data": {"hello": "world"}})
    p = run_runner(live, jid)
    assert p.returncode == 0, p.stdout + p.stderr
    j = fetch(jid)
    assert j.status == "completed" and json.loads(j.result_json) == {"echo": {"hello": "world"}}


def test_runner_disallowed_task_reports_failure(live):
    jid = make_remote({"task": "rm_rf"}, max_attempts=1)
    p = run_runner(live, jid)
    assert p.returncode == 1
    j = fetch(jid)
    assert j.status == "failed" and "not allowed" in j.error


def test_runner_with_wrong_secret_cannot_fetch_or_report(live):
    jid = make_remote({"task": "echo"})
    p = run_runner(live, jid, secret="attacker-guess")
    assert p.returncode != 0
    assert fetch(jid).status == "running"          # unauthorized caller changed nothing


def test_runner_logs_are_stored_with_job(live):
    jid = make_remote({"task": "echo", "data": 1})
    run_runner(live, jid)
    s = dbmod.session()
    try:
        from app.models import JobLog
        msgs = [x.message for x in s.query(JobLog).filter(JobLog.job_id == jid)]
    finally:
        s.close()
    assert any("running task echo" in m for m in msgs)
