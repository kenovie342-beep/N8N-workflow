"""Single outbound HTTP entry point (easy to mock in tests, scrubs secrets from errors)."""
import requests

from .logsafe import scrub

DEFAULT_TIMEOUT = 30


class RetryableError(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(scrub(message))
        self.retry_after = retry_after


class PermanentError(Exception):
    def __init__(self, message: str):
        super().__init__(scrub(message))


def retry_after_seconds(resp) -> float | None:
    v = resp.headers.get("Retry-After")
    if v:
        try:
            return max(0.0, float(v))
        except ValueError:
            return None
    return None


def request(method: str, url: str, *, secrets: tuple = (), **kw) -> requests.Response:
    """Perform a request. Network errors, 429 and 5xx -> RetryableError; other 4xx -> PermanentError."""
    kw.setdefault("timeout", DEFAULT_TIMEOUT)
    try:
        resp = requests.request(method, url, **kw)
    except requests.RequestException as exc:
        msg = str(exc)
        for s in secrets:
            if s:
                msg = msg.replace(s, "***")
        raise RetryableError(f"Network error: {msg[:300]}")
    if resp.status_code < 400:
        return resp
    body = resp.text[:400]
    for s in secrets:
        if s:
            body = body.replace(s, "***")
    if resp.status_code == 429 or resp.status_code >= 500:
        raise RetryableError(f"HTTP {resp.status_code}: {body}", retry_after_seconds(resp))
    hint = " (check token validity/permissions)" if resp.status_code in (401, 403) else ""
    raise PermanentError(f"HTTP {resp.status_code}{hint}: {body}")
