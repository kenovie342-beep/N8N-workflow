import os, hmac, hashlib
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Header, HTTPException

def cipher():
    key = os.getenv("APP_ENCRYPTION_KEY", "")
    if not key:
        raise RuntimeError("APP_ENCRYPTION_KEY is required")
    try:
        return Fernet(key.encode())
    except Exception as exc:
        raise RuntimeError("APP_ENCRYPTION_KEY must be a valid Fernet key") from exc

def encrypt(value: str) -> str:
    return cipher().encrypt(value.encode()).decode()

def decrypt(value: str) -> str:
    try:
        return cipher().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Cannot decrypt stored secret; check APP_ENCRYPTION_KEY") from exc

def mask(value: str) -> str:
    if not value: return ""
    return "••••••••" + value[-4:]

def verify_password(username: str, password: str) -> bool:
    return hmac.compare_digest(username, os.getenv("ADMIN_USERNAME", "admin")) and \
           hmac.compare_digest(password, os.getenv("ADMIN_PASSWORD", "change-this-password"))

def require_admin(authorization: str = Header(default="")):
    # Simple HTTP Basic auth; reverse proxy/TLS still required.
    import base64
    if not authorization.startswith("Basic "):
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate":"Basic"})
    try:
        raw = base64.b64decode(authorization.split(" ",1)[1]).decode()
        username, password = raw.split(":",1)
    except Exception:
        raise HTTPException(401, "Invalid credentials")
    if not verify_password(username, password):
        raise HTTPException(401, "Invalid credentials")
    return username

def require_n8n_token(x_addon_token: str = Header(default="")):
    expected = os.getenv("N8N_ADDON_API_TOKEN", "")
    if not expected or not hmac.compare_digest(x_addon_token, expected):
        raise HTTPException(401, "Invalid n8n add-on token")
    return True

def verify_callback(x_callback_secret: str = Header(default="")):
    expected = os.getenv("CALLBACK_SECRET", "")
    if not expected or not hmac.compare_digest(x_callback_secret, expected):
        raise HTTPException(401, "Invalid callback secret")
    return True
