import os

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

PG_URL = os.environ.get("TEST_PG_URL", "")
BACKENDS = ["sqlite"] + (["postgres"] if PG_URL else [])


@pytest.fixture(autouse=True)
def base_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("APP_SECRET_KEY", "s" * 48)
    monkeypatch.setenv("CALLBACK_SECRET", "cb-secret-for-tests-0123456789")
    monkeypatch.setenv("N8N_ADDON_API_TOKEN", "n8n-token-for-tests-0123456789")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct horse battery")
    monkeypatch.setenv("APP_DISABLE_WORKER", "1")
    monkeypatch.setenv("APP_AUTO_CREATE_TABLES", "1")
    monkeypatch.delenv("MAX_CONCURRENT_JOBS", raising=False)
    from app import ratelimit
    ratelimit.reset()


@pytest.fixture(params=BACKENDS)
def dburl(request, tmp_path, monkeypatch):
    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path}/test.db"
    else:
        url = PG_URL
    from app import models  # noqa: F401
    from app.db import Base
    eng = create_engine(url)
    Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    eng.dispose()
    monkeypatch.setenv("DATABASE_URL", url)
    from app import db as dbmod
    dbmod.configure(url)
    yield url
    dbmod.engine.dispose()


@pytest.fixture
def db(dburl):
    from app import db as dbmod
    s = dbmod.session()
    yield s
    s.close()


@pytest.fixture
def client(dburl):
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery"})
        assert r.status_code == 200
        c.headers["X-CSRF-Token"] = r.json()["csrf"]
        yield c
