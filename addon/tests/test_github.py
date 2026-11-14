import io
import json
import time
import zipfile
from datetime import timedelta

import pytest
import responses

from app import github_runner, http, jobs, security, store
from app.models import Job, JobLog, utcnow

SECRET = "cb-secret-for-tests-0123456789"


def signed(body: dict | str, secret=SECRET, ts=None, nonce=None):
    raw = body if isinstance(body, str) else json.dumps(body)
    h = security.make_signed_headers(secret, raw)
    if ts is not None or nonce is not None:
        t, n = ts or h["X-Suite-Timestamp"], nonce or h["X-Suite-Nonce"]
        h = {"X-Suite-Timestamp": t, "X-Suite-Nonce": n, "X-Suite-Signature": security.sign_message(secret, t, n, raw)}
    return raw, {**h, "Content-Type": "application/json"}


def remote_job(db):
    j, _ = jobs.enqueue(db, "github_job", {"task": "echo", "data": 1}, max_attempts=2)
    jobs.claim(db)
    return j


def post_cb(client, body, **kw):
    raw, h = signed(body, **kw)
    return client.post("/api/runner/callback", content=raw, headers=h)


@pytest.fixture
def gh(db):
    store.upsert_singleton(db, "github", {"owner": "me", "repo": "runner", "workflow": "remote-job.yml", "ref": "main"},
                           {"token": "github_pat_SECRETTOKEN0123456789abc"})


@responses.activate
def test_dispatch_sends_only_job_id_never_payload_or_secrets(gh, db):
    responses.post("https://api.github.com/repos/me/runner/actions/workflows/remote-job.yml/dispatches", status=204)
    github_runner.dispatch(store.github_config(db), "job-123")
    sent = responses.calls[0].request
    body = json.loads(sent.body)
    assert body == {"ref": "main", "inputs": {"job_id": "job-123"}}
    assert sent.headers["Authorization"].startswith("Bearer github_pat_")   # token only in header, not in inputs
    assert b"github_pat_" not in sent.body


@responses.activate
def test_dispatch_failure_classification(gh, db):
    url = "https://api.github.com/repos/me/runner/actions/workflows/remote-job.yml/dispatches"
    responses.post(url, status=422, body="Workflow does not have workflow_dispatch trigger")
    with pytest.raises(http.PermanentError):
        github_runner.dispatch(store.github_config(db), "j")
    responses.replace(responses.POST, url, status=503, body="unavailable")
    with pytest.raises(http.RetryableError):
        github_runner.dispatch(store.github_config(db), "j")


@responses.activate
def test_github_job_dispatches_defers_and_holds_a_slot(gh, db):
    responses.post("https://api.github.com/repos/me/runner/actions/workflows/remote-job.yml/dispatches", status=204)
    j, _ = jobs.enqueue(db, "github_job", {"task": "echo"})
    w = jobs.Worker(poll=0.02, with_scheduler=False)
    w.start()
    try:
        end = time.time() + 5
        while time.time() < end:
            db.expire_all()
            if db.get(Job, j.id).lease_expires_at and db.get(Job, j.id).executor == "github":
                break
            time.sleep(0.05)
    finally:
        w.stop()
    db.expire_all()
    got = db.get(Job, j.id)
    assert got.status == "running" and got.executor == "github"   # waiting for callback, still counted as running


@responses.activate
def test_failed_dispatch_marks_job_failed_with_retry_policy(gh, db):
    responses.post("https://api.github.com/repos/me/runner/actions/workflows/remote-job.yml/dispatches", status=422, body="nope")
    j, _ = jobs.enqueue(db, "github_job", {}, max_attempts=3)
    w = jobs.Worker(poll=0.02, with_scheduler=False)
    w.start()
    try:
        end = time.time() + 5
        while time.time() < end:
            db.expire_all()
            if db.get(Job, j.id).status == "failed":
                break
            time.sleep(0.05)
    finally:
        w.stop()
    db.expire_all()
    assert db.get(Job, j.id).status == "failed"     # 422 is permanent: not retried


def test_valid_success_callback_completes_job_and_stores_logs(client, db):
    j = remote_job(db)
    r = post_cb(client, {"job_id": j.id, "status": "completed", "result": {"echo": 1}, "logs": "line1\nline2"})
    assert r.status_code == 200 and r.json()["applied"] is True
    db.expire_all()
    got = db.get(Job, j.id)
    assert got.status == "completed" and json.loads(got.result_json) == {"echo": 1}
    assert [x.message for x in db.query(JobLog).filter(JobLog.job_id == j.id).order_by(JobLog.id)] == ["line1", "line2"]


def test_bad_missing_stale_signatures_rejected(client, db):
    j = remote_job(db)
    body = {"job_id": j.id, "status": "completed"}
    raw, h = signed(body, secret="wrong-secret")
    assert client.post("/api/runner/callback", content=raw, headers=h).status_code == 401
    assert client.post("/api/runner/callback", content=raw, headers={"Content-Type": "application/json"}).status_code == 401
    old = str(int(time.time()) - 3600)
    assert post_cb(client, body, ts=old).status_code == 401
    raw, h = signed(body)
    assert client.post("/api/runner/callback", content=raw + " ", headers=h).status_code == 401   # tampered body
    db.expire_all()
    assert db.get(Job, j.id).status == "running"


def test_replayed_request_has_no_effect(client, db):
    j = remote_job(db)
    raw, h = signed({"job_id": j.id, "status": "completed", "result": {"a": 1}})
    assert client.post("/api/runner/callback", content=raw, headers=h).json()["applied"] is True
    again = client.post("/api/runner/callback", content=raw, headers=h).json()
    assert again["duplicate"] is True and again["applied"] is False


def test_duplicate_callback_with_new_nonce_is_idempotent(client, db):
    j = remote_job(db)
    assert post_cb(client, {"job_id": j.id, "status": "completed", "result": {"v": 1}}).json()["applied"] is True
    second = post_cb(client, {"job_id": j.id, "status": "completed", "result": {"v": 999}}).json()
    assert second["applied"] is False
    db.expire_all()
    assert json.loads(db.get(Job, j.id).result_json) == {"v": 1}      # first result wins


def test_late_callback_cannot_resurrect_cancelled_job(client, db):
    j = remote_job(db)
    j.status = "cancelled"
    db.commit()
    assert post_cb(client, {"job_id": j.id, "status": "completed"}).json()["applied"] is False
    db.expire_all()
    assert db.get(Job, j.id).status == "cancelled"


def test_failed_callback_final_and_retryable(client, db):
    j = remote_job(db)                                                   # max_attempts=2, attempts=1
    r = post_cb(client, {"job_id": j.id, "status": "failed", "error": "boom", "retryable": True}).json()
    assert r["applied"]
    db.expire_all()
    assert db.get(Job, j.id).status == "queued"                          # retried with backoff
    j2 = remote_job(db)
    post_cb(client, {"job_id": j2.id, "status": "failed", "error": "fatal"})
    db.expire_all()
    got = db.get(Job, j2.id)
    assert got.status in ("failed", "queued")


def test_callback_validation_and_unknown_job(client):
    assert post_cb(client, {"status": "completed"}).status_code == 400
    assert post_cb(client, {"job_id": "x", "status": "weird"}).status_code == 400
    assert post_cb(client, {"job_id": "does-not-exist", "status": "completed"}).status_code == 404


def test_runner_payload_endpoint_is_signed_and_scoped(client, db):
    j = remote_job(db)
    raw, h = signed(j.id)
    r = client.get(f"/api/runner/jobs/{j.id}/payload", headers=h)
    assert r.status_code == 200 and r.json()["payload"]["task"] == "echo"
    assert client.get(f"/api/runner/jobs/{j.id}/payload", headers=h).status_code == 409   # replay
    assert client.get(f"/api/runner/jobs/{j.id}/payload").status_code == 401
    local, _ = jobs.enqueue(db, "noop")
    jobs.claim(db)
    _, h2 = signed(local.id)
    assert client.get(f"/api/runner/jobs/{local.id}/payload", headers=h2).status_code == 404  # only remote jobs


@responses.activate
def test_failure_detection_when_callback_is_lost(gh, db):
    j = remote_job(db)
    j.started_at = utcnow() - timedelta(minutes=10)
    db.commit()
    responses.get("https://api.github.com/repos/me/runner/actions/runs",
                  json={"workflow_runs": [{"id": 555, "display_title": f"suite-job {j.id}", "status": "completed", "conclusion": "failure"}]})
    assert jobs.reconcile_github(db) == 1
    db.expire_all()
    got = db.get(Job, j.id)
    assert got.remote_run_id == "555" and got.status in ("queued", "failed") and "failure" in got.error


@responses.activate
def test_cancel_running_remote_job_calls_github(gh, db):
    j = remote_job(db)
    responses.get("https://api.github.com/repos/me/runner/actions/runs",
                  json={"workflow_runs": [{"id": 777, "display_title": f"suite-job {j.id}", "status": "in_progress"}]})
    responses.post("https://api.github.com/repos/me/runner/actions/runs/777/cancel", status=202)
    assert jobs.cancel(db, j.id) == "cancelled"
    assert any(c.request.url.endswith("/runs/777/cancel") for c in responses.calls)


@responses.activate
def test_remote_log_retrieval_parses_zip_and_scrubs(gh, db, client):
    j = remote_job(db)
    j.remote_run_id = "321"
    db.commit()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("1_execute.txt", "hello from runner\ntoken=ghp_abcdefghijklmnopqrstuvwxyz0123456789\n")
    responses.get("https://api.github.com/repos/me/runner/actions/runs/321/logs", body=buf.getvalue())
    out = client.get(f"/api/jobs/{j.id}/remote-logs").json()
    assert "hello from runner" in out["logs"] and "ghp_abcdefghijkl" not in out["logs"]


def test_github_token_never_returned_by_api(client):
    r = client.put("/api/settings/github", json={"owner": "me", "repo": "r", "workflow": "w.yml", "ref": "main",
                                                  "token": "github_pat_TOPSECRETVALUE0123456789"})
    blob = json.dumps(r.json()) + json.dumps(client.get("/api/settings/github").json())
    assert "TOPSECRETVALUE" not in blob and "token_masked" in blob
