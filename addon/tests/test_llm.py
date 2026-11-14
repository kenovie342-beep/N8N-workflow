import json

import pytest
import responses

from app import http, llm


def test_build_request_per_provider():
    u, h, b = llm.build_request("deepseek", "deepseek-chat", "", "KEY123", "hi", "sys")
    assert u == "https://api.deepseek.com/v1/chat/completions" and h["Authorization"] == "Bearer KEY123"
    assert b["messages"][0] == {"role": "system", "content": "sys"} and b["model"] == "deepseek-chat"
    u, h, b = llm.build_request("openai", "gpt-4o-mini", "", "K", "hi")
    assert u.startswith("https://api.openai.com/v1/")
    u, h, b = llm.build_request("gemini", "gemini-2.0-flash", "", "GKEY", "hi")
    assert "GKEY" not in u and h["x-goog-api-key"] == "GKEY" and u.endswith(":generateContent")
    u, h, b = llm.build_request("anthropic", "claude-x", "", "AKEY", "hi", "s")
    assert u.endswith("/v1/messages") and h["x-api-key"] == "AKEY" and b["system"] == "s"
    u, h, b = llm.build_request("local", "llama3", "http://localhost:11434/v1", "", "hi")
    assert "Authorization" not in h and u == "http://localhost:11434/v1/chat/completions"
    with pytest.raises(http.PermanentError):
        llm.build_request("custom", "m", "", "k", "hi")


def test_parse_response_shapes():
    assert llm.parse_response("openai", {"choices": [{"message": {"content": "hey"}}]}) == "hey"
    assert llm.parse_response("gemini", {"candidates": [{"content": {"parts": [{"text": "a"}, {"text": "b"}]}}]}) == "ab"
    assert llm.parse_response("anthropic", {"content": [{"type": "text", "text": "yo"}]}) == "yo"
    with pytest.raises(http.PermanentError):
        llm.parse_response("openai", {"nope": 1})


@responses.activate
def test_generate_and_connection_test_success():
    responses.post("https://api.deepseek.com/v1/chat/completions", json={"choices": [{"message": {"content": "OK"}}]})
    out = llm.test_connection("deepseek", "deepseek-chat", "", "KEY")
    assert out["ok"] and out["sample"] == "OK"
    sent = responses.calls[0].request
    assert sent.headers["Authorization"] == "Bearer KEY" and json.loads(sent.body)["model"] == "deepseek-chat"


@responses.activate
def test_connection_failure_is_reported_without_leaking_key():
    responses.post("https://api.openai.com/v1/chat/completions", status=401, body="bad key sk-LEAKYKEY1234567890")
    out = llm.test_connection("openai", "gpt", "", "sk-LEAKYKEY1234567890")
    assert out["ok"] is False and "sk-LEAKYKEY" not in out["message"] and "401" in out["message"]


@responses.activate
def test_rate_limit_is_retryable_with_retry_after():
    responses.post("https://api.openai.com/v1/chat/completions", status=429, headers={"Retry-After": "42"}, body="slow down")
    with pytest.raises(http.RetryableError) as e:
        llm.generate("openai", "m", "", "k", "p")
    assert e.value.retry_after == 42


# ---- settings API ------------------------------------------------------------
def test_api_save_list_masked_update_default_delete(client):
    r = client.post("/api/settings/llm", json={"name": "DS", "provider": "deepseek", "model": "deepseek-chat",
                                                "api_key": "sk-abcdef1234567890WXYZ"})
    assert r.status_code == 200
    a = r.json()
    assert a["is_default"] is True and a["api_key_masked"].endswith("WXYZ")
    blob = json.dumps(client.get("/api/settings/llm").json()) + json.dumps(a)
    assert "sk-abcdef1234567890" not in blob
    # second provider, not default
    b = client.post("/api/settings/llm", json={"name": "OAI", "provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-other-000000000000"}).json()
    assert b["is_default"] is False
    client.post(f"/api/settings/llm/{b['id']}/default")
    ids = {x["id"]: x["is_default"] for x in client.get("/api/settings/llm").json()}
    assert ids[b["id"]] and not ids[a["id"]]
    # edit model without sending a key keeps the stored key
    u = client.put(f"/api/settings/llm/{a['id']}", json={"model": "deepseek-reasoner"}).json()
    assert u["model"] == "deepseek-reasoner" and u["api_key_masked"].endswith("WXYZ")
    assert client.delete(f"/api/settings/llm/{a['id']}").status_code == 200
    assert len(client.get("/api/settings/llm").json()) == 1


def test_api_rejects_bad_provider_and_missing_custom_endpoint(client):
    assert client.post("/api/settings/llm", json={"name": "x", "provider": "nope", "model": "m"}).status_code == 422
    assert client.post("/api/settings/llm", json={"name": "x", "provider": "custom", "model": "m", "api_key": "k"}).status_code == 400


def test_stored_key_is_encrypted_at_rest(client, db):
    from app.models import Setting
    client.post("/api/settings/llm", json={"name": "DS", "provider": "deepseek", "model": "m", "api_key": "sk-plaintext-check-123456"})
    row = db.query(Setting).filter(Setting.kind == "llm").one()
    assert "plaintext-check" not in row.secret_enc and "plaintext-check" not in row.config_json


@responses.activate
def test_api_connection_test_endpoint_and_per_workflow_model(client):
    pid = client.post("/api/settings/llm", json={"name": "DS", "provider": "deepseek", "model": "deepseek-chat", "api_key": "KEY-123456789"}).json()["id"]
    responses.post("https://api.deepseek.com/v1/chat/completions", json={"choices": [{"message": {"content": "OK"}}]})
    assert client.post(f"/api/settings/llm/{pid}/test").json()["ok"] is True
    # n8n generate with a per-workflow model override
    r = client.post("/api/n8n/generate", headers={"X-Addon-Token": "n8n-token-for-tests-0123456789"},
                    json={"prompt": "hello", "provider_id": pid, "model": "deepseek-reasoner"})
    assert r.status_code == 200 and r.json()["model"] == "deepseek-reasoner"
    assert json.loads(responses.calls[-1].request.body)["model"] == "deepseek-reasoner"
    assert client.post("/api/n8n/generate", json={"prompt": "x"}).status_code == 401
