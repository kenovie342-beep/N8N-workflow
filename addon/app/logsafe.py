"""Log scrubbing: never write credentials to logs, job logs or error messages."""
import logging
import re

_PATTERNS = [
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"(?i)(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}"),
    re.compile(r"(?i)(api[_-]?key|access_token|token|secret|password|key)=([^&\s\"']{4,})"),
]
_known: set[str] = set()


def register_secret(value: str) -> None:
    if value and len(value) >= 6:
        _known.add(value)


def scrub(text: str) -> str:
    if not text:
        return text
    for s in _known:
        text = text.replace(s, "***")
    for p in _PATTERNS:
        if p.groups >= 2:
            text = p.sub(lambda m: f"{m.group(1)}=***", text)
        elif p.groups == 1:
            text = p.sub(lambda m: f"{m.group(1)} ***", text)
        else:
            text = p.sub("***", text)
    return text


class ScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = scrub(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


def install() -> None:
    root = logging.getLogger()
    if not any(isinstance(f, ScrubFilter) for f in root.filters):
        root.addFilter(ScrubFilter())
    for h in root.handlers:
        if not any(isinstance(f, ScrubFilter) for f in h.filters):
            h.addFilter(ScrubFilter())
