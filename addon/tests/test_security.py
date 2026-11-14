import time

import pytest
from fastapi import HTTPException

from app import logsafe, security


def test_encrypt_decrypt_roundtrip_and_ciphertext_differs():
    c = security.encrypt("sk-secret-value-123456")
    assert "sk-secret" not in c
    assert security.decrypt(c) == "sk-secret-value-123456"


def test_decrypt_with_wrong_key_fails(monkeypatch):
    c = security.encrypt("abc")
    from cryptography.fernet import Fernet
    monkeypatch.setenv("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with pytest.raises(RuntimeError):
        security.decrypt(c)


def test_mask_never_reveals_short_secrets():
    assert security.mask("short") == "\u2022" * 8
    assert security.mask("a-long-secret-value-9876").endswith("9876")
    assert security.mask("") == ""


def test_login_requires_auth_and_csrf(client):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as anon:
        assert anon.get("/api/jobs").status_code == 401
        assert anon.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    # authenticated but missing CSRF header on a mutating request
    h = dict(client.headers)
    client.headers.pop("X-CSRF-Token")
    assert client.post("/api/jobs", json={"task": "noop"}).status_code == 403
    client.headers.update(h)
    assert client.post("/api/jobs", json={"task": "noop"}).status_code == 200


def test_tampered_or_expired_session_rejected(monkeypatch):
    tok, _ = security.make_session("admin")
    assert security.read_session(tok)
    assert security.read_session(tok[:-3] + "xxx") is None
    monkeypatch.setattr(time, "time", lambda: time.time.__wrapped__() + 10**6 if hasattr(time.time, "__wrapped__") else 4_000_000_000)
    assert security.read_session(tok) is None


def test_password_hash_login(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", security.hash_password("pw-12345-xyz", iterations=1000))
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    assert security.verify_login("admin", "pw-12345-xyz")
    assert not security.verify_login("admin", "nope")


def test_login_is_rate_limited(client):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as anon:
        codes = [anon.post("/api/auth/login", json={"username": "x", "password": "y"}).status_code for _ in range(12)]
    assert 429 in codes and codes[0] == 401


def test_signature_valid_stale_and_tampered():
    secret = "s3cret"
    h = security.make_signed_headers(secret, "body")
    lower = {k.lower(): v for k, v in h.items()}
    nonce, _ = security.verify_signature(secret, lower, "body")
    assert nonce
    with pytest.raises(HTTPException):
        security.verify_signature(secret, lower, "body-tampered")
    with pytest.raises(HTTPException):
        security.verify_signature("other", lower, "body")
    with pytest.raises(HTTPException):
        security.verify_signature(secret, lower, "body", now=time.time() + 1000)  # stale
    with pytest.raises(HTTPException):
        security.verify_signature(secret, {}, "body")


def test_nonce_replay_detected(db):
    assert security.register_nonce(db, "n1") is True
    assert security.register_nonce(db, "n1") is False


def test_logsafe_scrubs_known_and_pattern_secrets():
    logsafe.register_secret("my-very-private-value")
    out = logsafe.scrub("token my-very-private-value and github_pat_11ABCDEFGH0123456789abcdefghijkl "
                        "Authorization: Bearer abcdef1234567890 url?api_key=SUPERSECRET1 sk-abcdefghijklmnop1234")
    for leaked in ("my-very-private-value", "github_pat_11ABCDEF", "abcdef1234567890", "SUPERSECRET1", "sk-abcdefghijklmnop"):
        assert leaked not in out


def test_production_refuses_placeholder_secrets(monkeypatch):
    from app import config
    monkeypatch.setenv("APP_SECRET_KEY", "change-this-to-a-long-random-secret")
    assert any("APP_SECRET_KEY" in p for p in config.validate_production())
