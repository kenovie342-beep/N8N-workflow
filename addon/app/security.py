"""Encryption, sessions, password checks, HMAC request signing and replay protection."""
import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import timedelta

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request

from .config import env
from .models import CallbackNonce, utcnow

SESSION_TTL = 12 * 3600
SIGNATURE_MAX_SKEW = 300


# ---------------------------------------------------------------- encryption
def _fernet() -> Fernet:
    key = env("APP_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("APP_ENCRYPTION_KEY is required")
    try:
        return Fernet(key.encode())
    except Exception as exc:
        raise RuntimeError("APP_ENCRYPTION_KEY must be a valid Fernet key") from exc


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Cannot decrypt stored secret; check APP_ENCRYPTION_KEY") from exc


def encrypt_json(data: dict) -> str:
    return encrypt(json.dumps(data))


def decrypt_json(blob: str) -> dict:
    return json.loads(decrypt(blob)) if blob else {}


def mask(value: str) -> str:
    """Mask a secret for display. Only the last 4 characters (if long enough) are shown."""
    if not value:
        return ""
    return "\u2022" * 8 + (value[-4:] if len(value) >= 12 else "")


def mask_map(secrets_map: dict) -> dict:
    return {k: mask(str(v)) for k, v in secrets_map.items() if v}


# ------------------------------------------------------------------ passwords
def hash_password(password: str, iterations: int = 310_000) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def _check_hash(password: str, stored: str) -> bool:
    try:
        _, iters, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iters))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


def verify_login(username: str, password: str) -> bool:
    user_ok = hmac.compare_digest(username.encode(), env("ADMIN_USERNAME", "admin").encode())
    stored_hash = env("ADMIN_PASSWORD_HASH")
    if stored_hash:
        pw_ok = _check_hash(password, stored_hash)
    else:
        expected = env("ADMIN_PASSWORD")
        pw_ok = bool(expected) and hmac.compare_digest(password.encode(), expected.encode())
    return user_ok and pw_ok


# ------------------------------------------------------------------- sessions
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(data: bytes) -> str:
    return _b64(hmac.new(env("APP_SECRET_KEY").encode(), data, hashlib.sha256).digest())


def make_session(username: str) -> tuple[str, str]:
    csrf = secrets.token_urlsafe(24)
    payload = _b64(json.dumps({"u": username, "exp": int(time.time()) + SESSION_TTL, "csrf": csrf}).encode())
    return f"{payload}.{_sign(payload.encode())}", csrf


def read_session(token: str | None) -> dict | None:
    if not token or "." not in token or not env("APP_SECRET_KEY"):
        return None
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(payload.encode())):
        return None
    try:
        data = json.loads(_unb64(payload))
    except Exception:
        return None
    if data.get("exp", 0) < time.time():
        return None
    return data


def require_admin(request: Request) -> str:
    """Session cookie auth + CSRF header on state-changing requests."""
    data = read_session(request.cookies.get("suite_session"))
    if not data:
        raise HTTPException(401, "Authentication required")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        sent = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(sent.encode(), str(data.get("csrf", "")).encode()):
            raise HTTPException(403, "Missing or invalid CSRF token")
    return data["u"]


def require_n8n_token(request: Request) -> bool:
    expected = env("N8N_ADDON_API_TOKEN")
    got = request.headers.get("x-addon-token", "")
    if not expected or not hmac.compare_digest(got.encode(), expected.encode()):
        raise HTTPException(401, "Invalid add-on token")
    return True


# ------------------------------------------------- HMAC signing (callbacks etc.)
def sign_message(secret: str, timestamp: str, nonce: str, body: str) -> str:
    msg = f"{timestamp}.{nonce}.{body}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def make_signed_headers(secret: str, body: str) -> dict:
    ts, nonce = str(int(time.time())), secrets.token_hex(12)
    return {
        "X-Suite-Timestamp": ts,
        "X-Suite-Nonce": nonce,
        "X-Suite-Signature": sign_message(secret, ts, nonce, body),
    }


def verify_signature(secret: str, headers, body: str, now: float | None = None) -> tuple[str, str]:
    """Validate signature + freshness. Returns (nonce, timestamp). Raises HTTPException(401)."""
    if not secret:
        raise HTTPException(401, "Signing secret not configured")
    ts = headers.get("x-suite-timestamp", "")
    nonce = headers.get("x-suite-nonce", "")
    sig = headers.get("x-suite-signature", "")
    if not (ts and nonce and sig):
        raise HTTPException(401, "Missing signature headers")
    try:
        age = abs((now or time.time()) - int(ts))
    except ValueError:
        raise HTTPException(401, "Invalid timestamp")
    if age > SIGNATURE_MAX_SKEW:
        raise HTTPException(401, "Stale request")
    if not hmac.compare_digest(sig, sign_message(secret, ts, nonce, body)):
        raise HTTPException(401, "Invalid signature")
    return nonce, ts


def register_nonce(db, nonce: str, job_id: str = "") -> bool:
    """Record a nonce. Returns False if it was already seen (replay)."""
    from sqlalchemy.exc import IntegrityError

    db.query(CallbackNonce).filter(CallbackNonce.seen_at < utcnow() - timedelta(seconds=SIGNATURE_MAX_SKEW * 2)).delete()
    db.add(CallbackNonce(nonce=nonce, job_id=job_id))
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False
