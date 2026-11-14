from .base import LIMITS, Platform, format_text
from .facebook import Facebook
from .instagram import Instagram
from .linkedin import LinkedIn
from .pinterest import Pinterest
from .reddit import Reddit
from .threads import Threads
from .x import X
from .youtube import YouTube

REGISTRY: dict[str, type[Platform]] = {
    c.name: c for c in (Reddit, Facebook, Instagram, YouTube, X, LinkedIn, Threads, Pinterest)
}


def spec() -> dict:
    return {n: {"required": list(c.required), "optional": list(c.optional), "permissions": c.permissions,
                "media": c.media, "notes": c.notes, "text_limit": LIMITS.get(n)} for n, c in REGISTRY.items()}


def get(name: str, creds: dict, config: dict | None = None) -> Platform:
    if name not in REGISTRY:
        raise ValueError(f"Unsupported platform: {name}")
    return REGISTRY[name](creds, config)


__all__ = ["REGISTRY", "spec", "get", "format_text", "LIMITS"]
