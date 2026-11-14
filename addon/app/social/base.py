"""Shared pieces for the eight social modules."""
from .. import http

# Platform-specific text limits used by the formatter.
LIMITS: dict[str, int] = {
    "x": 280, "threads": 500, "linkedin": 3000, "instagram": 2200,
    "facebook": 63000, "reddit": 40000, "pinterest": 800, "youtube": 5000,
}
TITLE_LIMITS: dict[str, int] = {"reddit": 300, "pinterest": 100, "youtube": 100}


class Platform:
    name = ""
    required: tuple[str, ...] = ()        # credential fields that must be present
    optional: tuple[str, ...] = ()
    permissions = ""     # documented API permissions/scopes
    media = "none"       # what media is supported by this module
    notes = ""

    def __init__(self, creds: dict, config: dict | None = None):
        self.creds = creds
        self.config = config or {}

    @classmethod
    def validate(cls, creds: dict) -> list[str]:
        return [f for f in cls.required if not creds.get(f)]

    def secret_values(self) -> tuple:
        return tuple(str(v) for v in self.creds.values() if v)

    def call(self, method: str, url: str, **kw):
        return http.request(method, url, secrets=self.secret_values(), **kw)

    def test_connection(self) -> dict:  # pragma: no cover - overridden
        raise NotImplementedError

    def publish(self, payload: dict) -> dict:  # pragma: no cover - overridden
        raise NotImplementedError


def format_text(platform: str, text: str, title: str = "") -> dict:
    """Platform-specific formatting: whitespace cleanup + safe truncation."""
    text = (text or "").strip()
    limit = LIMITS.get(platform, 2000)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "\u2026"
    out = {"text": text}
    tl = TITLE_LIMITS.get(platform)
    if tl:
        t = (title or text.split("\n")[0]).strip()
        out["title"] = t if len(t) <= tl else t[: tl - 1].rstrip() + "\u2026"
    return out
