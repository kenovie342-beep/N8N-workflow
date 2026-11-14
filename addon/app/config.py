"""Environment-driven configuration. Values are read lazily so tests can override them."""
import os


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def is_production() -> bool:
    return env("APP_ENV", "production").lower() == "production"


def validate_production() -> list[str]:
    """Return configuration problems that must block startup in production."""
    problems = []
    for name in ("APP_SECRET_KEY", "APP_ENCRYPTION_KEY", "CALLBACK_SECRET", "N8N_ADDON_API_TOKEN"):
        v = env(name)
        if not v or v.startswith("change-") or v.startswith("generate-"):
            problems.append(f"{name} is not set to a real secret")
    pw = env("ADMIN_PASSWORD")
    if not (env("ADMIN_PASSWORD_HASH") or (pw and not pw.startswith("change-"))):
        problems.append("ADMIN_PASSWORD (or ADMIN_PASSWORD_HASH) is not set")
    return problems
