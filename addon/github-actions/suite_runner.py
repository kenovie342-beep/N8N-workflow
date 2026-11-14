#!/usr/bin/env python3
"""Remote job runner executed inside GitHub Actions. Standard library only.

Flow: fetch payload (HMAC-signed GET) -> run an allow-listed task -> post signed callback with result + logs.
Secrets used: SUITE_URL, CALLBACK_SECRET (repository secrets); optional CALLBACK_URL (e.g. the Cloudflare Worker /callback). Nothing sensitive is passed as workflow inputs.
"""
import hashlib
import hmac
import io
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

MAX_LOG = 20000
log_buf = io.StringIO()


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    log_buf.write(line + "\n")


def sign(secret: str, body: str) -> dict:
    ts, nonce = str(int(time.time())), secrets.token_hex(12)
    sig = hmac.new(secret.encode(), f"{ts}.{nonce}.{body}".encode(), hashlib.sha256).hexdigest()
    return {"X-Suite-Timestamp": ts, "X-Suite-Nonce": nonce, "X-Suite-Signature": sig}


def call(method: str, url: str, secret: str, body: str, timeout: int = 30) -> dict:
    headers = {"Content-Type": "application/json", **sign(secret, body)}
    req = urllib.request.Request(url, data=body.encode() if method == "POST" else None, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - URL comes from our own secret
        return json.loads(r.read().decode() or "{}")


# ------------------------------------------------------------------ tasks (allow-list)
def t_echo(p: dict) -> dict:
    log(f"echo: {json.dumps(p.get('data'))[:200]}")
    return {"echo": p.get("data")}


def t_sleep(p: dict) -> dict:
    s = min(float(p.get("seconds", 1)), 600)
    log(f"sleeping {s}s")
    time.sleep(s)
    return {"slept": s}


def t_http_check(p: dict) -> dict:
    url = str(p.get("url", ""))
    if not url.startswith(("http://", "https://")):
        raise ValueError("url must be http(s)")
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=int(p.get("timeout", 15))) as r:  # noqa: S310
            status, size = r.status, len(r.read(2_000_000))
    except urllib.error.HTTPError as e:
        status, size = e.code, 0
    log(f"GET {url} -> {status}")
    return {"url": url, "status": status, "bytes": size, "elapsed_ms": int((time.time() - t0) * 1000)}


TASKS = {"echo": t_echo, "sleep": t_sleep, "http_check": t_http_check}


def main() -> int:
    job_id, base, secret = os.environ.get("JOB_ID", ""), os.environ.get("SUITE_URL", "").rstrip("/"), os.environ.get("CALLBACK_SECRET", "")
    if not (job_id and base and secret):
        print("JOB_ID, SUITE_URL and CALLBACK_SECRET are required", file=sys.stderr)
        return 2
    result, status, error, retryable = {}, "completed", "", False
    try:
        payload = call("GET", f"{base}/api/runner/jobs/{job_id}/payload", secret, job_id)["payload"]
        name = payload.get("task")
        if name not in TASKS:
            raise ValueError(f"task '{name}' is not allowed on the runner (allowed: {', '.join(TASKS)})")
        log(f"running task {name}")
        result = TASKS[name](payload)
    except ValueError as e:
        status, error = "failed", str(e)
    except Exception as e:  # network/infra problems are worth retrying
        status, error, retryable = "failed", f"{type(e).__name__}: {e}", True
    body = json.dumps({"job_id": job_id, "status": status, "result": result, "error": error,
                       "retryable": retryable, "logs": log_buf.getvalue()[-MAX_LOG:]})
    for attempt in range(1, int(os.environ.get("CALLBACK_MAX_ATTEMPTS", "5")) + 1):
        try:
            print("callback:", call("POST", os.environ.get("CALLBACK_URL") or f"{base}/api/runner/callback", secret, body))
            return 0 if status == "completed" else 1
        except Exception as e:
            print(f"callback attempt {attempt} failed: {type(e).__name__}", file=sys.stderr)
            time.sleep(min(2 ** attempt, 20))
    return 3


if __name__ == "__main__":
    sys.exit(main())
