"""Real process-level restart test: separate uvicorn processes, SIGKILL, same database."""
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text

from tests.conftest import PG_URL

ROOT = Path(__file__).resolve().parent.parent


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def server_env(url, key):
    base = {k: v for k, v in os.environ.items() if k not in ("APP_DISABLE_WORKER", "APP_AUTO_CREATE_TABLES")}
    return {**base, "APP_ENV": "production", "DATABASE_URL": url, "APP_ENCRYPTION_KEY": key, "APP_SECRET_KEY": "k" * 48,
            "CALLBACK_SECRET": "cb-" + "c" * 30, "N8N_ADDON_API_TOKEN": "t-" + "t" * 30, "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "a-real-password-1", "WORKER_POLL_SECONDS": "0.1", "PYTHONPATH": str(ROOT)}


def start(env, port):
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
                         cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(150):
        try:
            if requests.get(f"http://127.0.0.1:{port}/health", timeout=1).ok:
                return p
        except requests.RequestException:
            time.sleep(0.1)
    p.kill()
    raise RuntimeError(p.stdout.read().decode()[-2000:])


def login(port):
    s = requests.Session()
    r = s.post(f"http://127.0.0.1:{port}/api/auth/login", json={"username": "admin", "password": "a-real-password-1"})
    r.raise_for_status()
    s.headers["X-CSRF-Token"] = r.json()["csrf"]
    return s


@pytest.fixture(params=["sqlite"] + (["pg"] if PG_URL else []))
def dburl(request, tmp_path):
    url = f"sqlite:///{tmp_path}/r.db" if request.param == "sqlite" else PG_URL
    if request.param == "pg":
        eng = create_engine(url)
        with eng.begin() as c:
            c.execute(text("DROP SCHEMA public CASCADE")); c.execute(text("CREATE SCHEMA public"))
        eng.dispose()
    return url


def test_kill9_restart_keeps_schedules_jobs_settings_and_recovers_running_job(dburl):
    key = Fernet.generate_key().decode()
    env = server_env(dburl, key)
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True, capture_output=True)
    port = free_port()
    a = start(env, port)
    try:
        s = login(port); base = f"http://127.0.0.1:{port}"
        assert s.put(f"{base}/api/settings/app", json={"max_concurrency": 1}).status_code == 200
        sched = s.post(f"{base}/api/schedules", json={"name": "daily", "task": "noop", "kind": "cron", "cron_expr": "0 9 * * *"}).json()
        s.post(f"{base}/api/settings/llm", json={"name": "DS", "provider": "deepseek", "model": "deepseek-chat", "api_key": "sk-persist-0123456789"}).json()
        running = s.post(f"{base}/api/jobs", json={"task": "sleep", "payload": {"seconds": 60}}).json()["id"]
        queued = s.post(f"{base}/api/jobs", json={"task": "sleep", "payload": {"seconds": 60}}).json()["id"]
        for _ in range(100):
            if s.get(f"{base}/api/jobs/{running}").json()["status"] == "running":
                break
            time.sleep(0.1)
        assert s.get(f"{base}/api/jobs/{queued}").json()["status"] == "queued"      # limit 1 holds the second job
    finally:
        a.send_signal(signal.SIGKILL); a.wait()                                       # hard crash, no graceful shutdown

    port2 = free_port()
    b = start(env, port2)
    try:
        s2 = login(port2); base = f"http://127.0.0.1:{port2}"
        assert s2.get(f"{base}/api/settings/app").json()["max_concurrency"] == 1
        assert [x["id"] for x in s2.get(f"{base}/api/schedules").json()] == [sched["id"]]
        assert s2.get(f"{base}/api/schedules").json()[0]["next_run_at"]
        assert s2.get(f"{base}/api/settings/llm").json()[0]["api_key_masked"].endswith("6789")
        time.sleep(1.5)
        j1, j2 = (s2.get(f"{base}/api/jobs/{i}").json() for i in (running, queued))
        assert {j1["status"], j2["status"]} <= {"running", "queued"}, "jobs must not be lost or failed by the crash"
        assert j1["attempts"] >= 1 and "Interrupted" in j1["error"]                          # recovered (retry is backed off)
        assert j2["status"] == "running"                                                 # the waiting job took the free slot
        assert s2.get(f"{base}/api/stats").json()["total"] == 2
        for i in (running, queued):
            s2.post(f"{base}/api/jobs/{i}/cancel")
    finally:
        b.send_signal(signal.SIGTERM); b.wait(timeout=15)


def test_production_refuses_to_start_with_placeholder_secrets(tmp_path):
    env = server_env(f"sqlite:///{tmp_path}/x.db", Fernet.generate_key().decode())
    env["APP_SECRET_KEY"] = "change-this-to-a-long-random-secret"
    p = subprocess.run([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(free_port())], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=30)
    assert p.returncode != 0 and "Refusing to start" in (p.stdout + p.stderr)
