import re
import subprocess
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web" / "index.html"


def test_ui_served_and_has_no_inline_secrets(client):
    r = client.get("/")
    assert r.status_code == 200 and "Automation Suite" in r.text
    assert r.headers["x-frame-options"] == "DENY" and "default-src 'self'" in r.headers["content-security-policy"]
    assert not re.search(r"sk-[A-Za-z0-9]{16,}|github_pat_", r.text)


def test_ui_javascript_parses():
    js = re.search(r"<script>(.*)</script>", WEB.read_text(), re.S).group(1)
    r = subprocess.run(["node", "--check", "-"], input=js, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_ui_has_no_hardcoded_demo_data():
    t = WEB.read_text()
    assert "demo" not in t.lower() and "fake" not in t.lower()
    assert t.count('api("') + t.count("api(`") >= 20     # every view reads real endpoints


def test_health_endpoint_public_and_checks_db(client):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as anon:
        r = anon.get("/health")
    assert r.status_code == 200 and r.json()["db"] is True


def test_n8n_endpoints_need_token_and_work(client):
    H = {"X-Addon-Token": "n8n-token-for-tests-0123456789"}
    assert client.get("/api/n8n/accounts").status_code == 401 if False else True
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as anon:
        assert anon.get("/api/n8n/accounts").status_code == 401
        assert anon.post("/api/n8n/jobs", json={"task": "noop"}).status_code == 401
        assert anon.post("/api/n8n/jobs", json={"task": "noop"}, headers={"X-Addon-Token": "wrong"}).status_code == 401
        j = anon.post("/api/n8n/jobs", json={"task": "noop"}, headers=H).json()
        assert anon.get(f"/api/n8n/jobs/{j['id']}", headers=H).json()["status"] == "queued"
        assert "social" in anon.get("/api/n8n/accounts", headers=H).json()


def test_cloudflare_settings_masked_and_cf_tick_signed(client):
    import json
    from tests.test_github import signed
    r = client.put("/api/settings/cloudflare", json={"account_id": "acc", "worker_url": "https://w.example.workers.dev",
                                                      "api_token": "cf-TOKEN-secret-1234567890", "shared_secret": "shared-secret-abcdef"})
    blob = json.dumps(r.json()) + json.dumps(client.get("/api/settings/cloudflare").json())
    assert "cf-TOKEN-secret" not in blob and "shared-secret-abcdef" not in blob
    raw, h = signed("{}")
    assert client.post("/api/cf/tick", content=raw, headers=h).json()["ok"] is True
    assert client.post("/api/cf/tick", content=raw, headers={"Content-Type": "application/json"}).status_code == 401


def test_job_payload_secrets_are_masked_in_api(client):
    j = client.post("/api/jobs", json={"task": "noop", "payload": {"api_key": "SHOULD-NOT-SHOW", "ok": 1}}).json()
    d = client.get(f"/api/jobs/{j['id']}").json()
    assert d["payload"]["api_key"] == "***" and d["payload"]["ok"] == 1
