"""Server-side credential retrieval. Decryption happens only here, at the point of use."""
import json

from sqlalchemy import select

from . import logsafe
from .config import env, env_int
from .models import AppSetting, Setting, utcnow
from .security import decrypt, decrypt_json, encrypt, encrypt_json


# ---------------------------------------------------------------- app settings
def get_app(db, key: str, default: str = "") -> str:
    row = db.get(AppSetting, key)
    return row.value if row else default


def set_app(db, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))
    db.commit()


def get_max_concurrency(db) -> int:
    v = get_app(db, "max_concurrency", "")
    n = int(v) if v.isdigit() else env_int("MAX_CONCURRENT_JOBS", 5)
    return max(1, min(n, 100))


def get_timezone(db) -> str:
    return get_app(db, "default_timezone", "") or env("DEFAULT_TIMEZONE", "Africa/Lagos")


# ------------------------------------------------------------------------ LLM
def llm_resolve(db, provider_id: str | None = None, model: str | None = None) -> dict:
    """Return {provider, model, endpoint, key}. Uses the default provider when no id is given."""
    if provider_id:
        s = db.get(Setting, provider_id)
    else:
        s = db.scalars(select(Setting).where(Setting.kind == "llm", Setting.is_default == True)).first()  # noqa: E712
        if not s:
            s = db.scalars(select(Setting).where(Setting.kind == "llm")).first()
    if not s or s.kind != "llm":
        raise LookupError("LLM provider not found (add one in Settings -> AI Providers)")
    cfg = json.loads(s.config_json)
    key = decrypt(s.secret_enc) if s.secret_enc else ""
    logsafe.register_secret(key)
    return {"id": s.id, "provider": cfg["provider"], "model": model or cfg["model"],
            "endpoint": cfg.get("endpoint", ""), "key": key}


# --------------------------------------------------------------------- social
def social_resolve(db, account_id: str) -> tuple[str, dict, dict]:
    s = db.get(Setting, account_id)
    if not s or s.kind != "social":
        raise LookupError("Social account not found")
    creds = decrypt_json(s.secret_enc)
    for v in creds.values():
        logsafe.register_secret(str(v))
    return s.name, creds, json.loads(s.config_json)


# --------------------------------------------------------------------- github
def github_config(db) -> dict:
    s = db.scalars(select(Setting).where(Setting.kind == "github")).first()
    cfg = {"owner": env("GITHUB_OWNER"), "repo": env("GITHUB_REPO"), "workflow": env("GITHUB_WORKFLOW_ID", "remote-job.yml"),
           "ref": env("GITHUB_REF", "main"), "token": env("GITHUB_TOKEN")}
    if s:
        stored = json.loads(s.config_json)
        cfg.update({k: v for k, v in stored.items() if v})
        if s.secret_enc:
            cfg["token"] = decrypt_json(s.secret_enc).get("token", cfg["token"])
    logsafe.register_secret(cfg["token"])
    return cfg


# ----------------------------------------------------------------- cloudflare
def cloudflare_config(db) -> dict:
    s = db.scalars(select(Setting).where(Setting.kind == "cloudflare")).first()
    if not s:
        return {}
    cfg = json.loads(s.config_json)
    cfg.update(decrypt_json(s.secret_enc))
    for k in ("api_token", "shared_secret"):
        logsafe.register_secret(cfg.get(k, ""))
    return cfg


def upsert_singleton(db, kind: str, config: dict, secrets_map: dict) -> Setting:
    """Create/update a one-row settings kind (github, cloudflare). Blank secret values keep the old ones."""
    s = db.scalars(select(Setting).where(Setting.kind == kind)).first()
    if s:
        old = decrypt_json(s.secret_enc)
        old.update({k: v for k, v in secrets_map.items() if v})
        s.secret_enc = encrypt_json(old)
        s.config_json = json.dumps(config)
        s.updated_at = utcnow()
    else:
        s = Setting(kind=kind, name=kind, config_json=json.dumps(config), secret_enc=encrypt_json(secrets_map))
        db.add(s)
    db.commit()
    return s


__all__ = ["encrypt", "decrypt"]
