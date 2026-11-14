"""Social module tests. ALL HTTP is MOCKED (responses). These do NOT prove live API publishing works."""
import json
import time
from urllib.parse import parse_qs

import pytest
import responses

from app import http, jobs, social
from app.social import format_text

G, T = "https://graph.facebook.com/v21.0", "https://graph.threads.net/v1.0"


def body_of(call) -> dict:
    b = call.request.body
    b = b.decode() if isinstance(b, bytes) else (b or "")
    try:
        return json.loads(b)
    except ValueError:
        return {k: v[0] for k, v in parse_qs(b).items()}


# ---------------------------------------------------------------- Reddit
@responses.activate
def test_reddit_connection_and_publish_self_post():
    c = {"client_id": "cid", "client_secret": "csec", "refresh_token": "rt"}
    responses.post("https://www.reddit.com/api/v1/access_token", json={"access_token": "AT"})
    responses.get("https://oauth.reddit.com/api/v1/me", json={"name": "bot"})
    assert social.get("reddit", c).test_connection() == {"ok": True, "account": "bot"}
    responses.post("https://oauth.reddit.com/api/submit", json={"json": {"errors": [], "data": {"id": "abc", "url": "https://reddit.com/r/x/abc"}}})
    out = social.get("reddit", c).publish({"subreddit": "test", "title": "Hi", "text": "body"})
    assert out == {"id": "abc", "url": "https://reddit.com/r/x/abc"}
    sub = body_of(responses.calls[-1])
    assert sub["sr"] == "test" and sub["kind"] == "self" and sub["text"] == "body"
    assert responses.calls[-1].request.headers["Authorization"] == "Bearer AT"
    assert responses.calls[0].request.headers["Authorization"].startswith("Basic ")   # refresh uses client basic auth


@responses.activate
def test_reddit_errors_and_ratelimit():
    c = {"client_id": "a", "client_secret": "b", "access_token": "AT"}
    responses.post("https://oauth.reddit.com/api/submit", json={"json": {"errors": [["RATELIMIT", "slow", "x"]]}})
    with pytest.raises(http.RetryableError):
        social.get("reddit", c).publish({"subreddit": "t", "title": "x"})
    responses.replace(responses.POST, "https://oauth.reddit.com/api/submit", json={"json": {"errors": [["SUBREDDIT_NOEXIST", "no", "sr"]]}})
    with pytest.raises(http.PermanentError):
        social.get("reddit", c).publish({"subreddit": "t", "title": "x"})
    with pytest.raises(http.PermanentError):
        social.get("reddit", c).publish({"title": "x"})   # subreddit missing


# -------------------------------------------------------------- Facebook
@responses.activate
def test_facebook_text_photo_video_and_scheduled():
    c = {"page_id": "P1", "page_access_token": "PTOK"}
    responses.get(f"{G}/P1", json={"id": "P1", "name": "My Page"})
    assert social.get("facebook", c).test_connection()["account"] == "My Page"
    responses.post(f"{G}/P1/feed", json={"id": "P1_1"})
    assert social.get("facebook", c).publish({"text": "hello", "link": "https://x.y"}) == {"id": "P1_1"}
    d = body_of(responses.calls[-1])
    assert d["message"] == "hello" and d["link"] == "https://x.y" and d["access_token"] == "PTOK"
    responses.post(f"{G}/P1/photos", json={"post_id": "P1_2"})
    assert social.get("facebook", c).publish({"text": "cap", "media_url": "https://i/p.jpg"})["id"] == "P1_2"
    assert body_of(responses.calls[-1])["url"] == "https://i/p.jpg"
    responses.post(f"{G}/P1/videos", json={"id": "V1"})
    social.get("facebook", c).publish({"text": "v", "media_url": "https://i/v.mp4", "media_type": "video", "scheduled_publish_time": 1900000000})
    d = body_of(responses.calls[-1])
    assert d["file_url"] == "https://i/v.mp4" and d["published"] == "false" and d["scheduled_publish_time"] == "1900000000"


@responses.activate
def test_facebook_error_mapping():
    c = {"page_id": "P1", "page_access_token": "PTOK"}
    responses.post(f"{G}/P1/feed", status=401, body="token PTOK expired")
    with pytest.raises(http.PermanentError) as e:
        social.get("facebook", c).publish({"text": "x"})
    assert "PTOK" not in str(e.value) and "permissions" in str(e.value)
    responses.replace(responses.POST, f"{G}/P1/feed", status=429, headers={"Retry-After": "120"})
    with pytest.raises(http.RetryableError) as e2:
        social.get("facebook", c).publish({"text": "x"})
    assert e2.value.retry_after == 120


# ------------------------------------------------------------- Instagram
@responses.activate
def test_instagram_image_two_step_publish_and_requires_media():
    c = {"ig_user_id": "IG1", "access_token": "TOK"}
    responses.get(f"{G}/IG1", json={"id": "IG1", "username": "brand"})
    assert social.get("instagram", c).test_connection()["account"] == "brand"
    responses.post(f"{G}/IG1/media", json={"id": "CONT1"})
    responses.post(f"{G}/IG1/media_publish", json={"id": "MEDIA9"})
    assert social.get("instagram", c).publish({"text": "cap", "media_url": "https://i/p.jpg"}) == {"id": "MEDIA9"}
    assert body_of(responses.calls[-2])["image_url"] == "https://i/p.jpg"
    assert body_of(responses.calls[-1])["creation_id"] == "CONT1"
    with pytest.raises(http.PermanentError):
        social.get("instagram", c).publish({"text": "no media"})


@responses.activate
def test_instagram_reel_polls_until_finished():
    c = {"ig_user_id": "IG1", "access_token": "TOK"}
    responses.post(f"{G}/IG1/media", json={"id": "C2"})
    responses.get(f"{G}/C2", json={"status_code": "IN_PROGRESS"})
    responses.get(f"{G}/C2", json={"status_code": "FINISHED"})
    responses.post(f"{G}/IG1/media_publish", json={"id": "R1"})
    out = social.get("instagram", c, {"poll_seconds": 0}).publish({"text": "t", "media_url": "https://i/v.mp4", "media_type": "video"})
    assert out["id"] == "R1" and body_of(responses.calls[0])["media_type"] == "REELS"
    responses.replace(responses.GET, f"{G}/C2", json={"status_code": "ERROR"})
    with pytest.raises(http.PermanentError):
        social.get("instagram", c, {"poll_seconds": 0}).publish({"text": "t", "media_url": "https://i/v.mp4", "media_type": "video"})


# --------------------------------------------------------------- Threads
@responses.activate
def test_threads_text_publish_flow():
    c = {"user_id": "U1", "access_token": "TT"}
    responses.get(f"{T}/me", json={"id": "U1", "username": "thr"})
    assert social.get("threads", c).test_connection()["account"] == "thr"
    responses.post(f"{T}/U1/threads", json={"id": "CT1"})
    responses.post(f"{T}/U1/threads_publish", json={"id": "TH9"})
    assert social.get("threads", c).publish({"text": "hello"}) == {"id": "TH9"}
    assert body_of(responses.calls[-2])["media_type"] == "TEXT" and body_of(responses.calls[-1])["creation_id"] == "CT1"


@responses.activate
def test_threads_image_waits_for_processing():
    c = {"user_id": "U1", "access_token": "TT"}
    responses.post(f"{T}/U1/threads", json={"id": "CT2"})
    responses.get(f"{T}/CT2", json={"status": "FINISHED"})
    responses.post(f"{T}/U1/threads_publish", json={"id": "TH2"})
    social.get("threads", c, {"poll_seconds": 0}).publish({"text": "x", "media_url": "https://i/p.png"})
    assert body_of(responses.calls[0])["media_type"] == "IMAGE"


# ------------------------------------------------------------- Pinterest
@responses.activate
def test_pinterest_pin_construction_and_validation():
    c = {"access_token": "PIN", "board_id": "B1"}
    responses.get("https://api.pinterest.com/v5/user_account", json={"username": "pinner"})
    assert social.get("pinterest", c).test_connection()["account"] == "pinner"
    responses.post("https://api.pinterest.com/v5/pins", json={"id": "PIN77"})
    out = social.get("pinterest", c).publish({"text": "desc", "title": "T", "media_url": "https://i/p.jpg", "link": "https://s"})
    assert out == {"id": "PIN77"}
    b = body_of(responses.calls[-1])
    assert b["board_id"] == "B1" and b["media_source"] == {"source_type": "image_url", "url": "https://i/p.jpg"} and b["title"] == "T"
    with pytest.raises(http.PermanentError):
        social.get("pinterest", c).publish({"text": "no image"})


# ---------------------------------------------------------------------- X
@responses.activate
def test_x_text_and_media_tweet():
    c = {"access_token": "XTOK"}
    responses.get("https://api.x.com/2/users/me", json={"data": {"username": "me_x"}})
    assert social.get("x", c).test_connection()["account"] == "me_x"
    responses.post("https://api.x.com/2/tweets", json={"data": {"id": "123"}})
    out = social.get("x", c).publish({"text": "hi"})
    assert out["id"] == "123" and body_of(responses.calls[-1]) == {"text": "hi"}
    assert responses.calls[-1].request.headers["Authorization"] == "Bearer XTOK"
    responses.get("https://i/p.jpg", body=b"\xff\xd8img", content_type="image/jpeg")
    responses.post("https://api.x.com/2/media/upload", json={"data": {"id": "M1"}})
    social.get("x", c).publish({"text": "pic", "media_url": "https://i/p.jpg"})
    assert body_of(responses.calls[-1])["media"] == {"media_ids": ["M1"]}


@responses.activate
def test_x_rate_limit_and_auth_errors():
    c = {"access_token": "XTOK"}
    responses.post("https://api.x.com/2/tweets", status=429, headers={"Retry-After": "900"}, body="Too Many Requests")
    with pytest.raises(http.RetryableError) as e:
        social.get("x", c).publish({"text": "hi"})
    assert e.value.retry_after == 900
    responses.replace(responses.POST, "https://api.x.com/2/tweets", status=403, body="app-only token cannot post")
    with pytest.raises(http.PermanentError):
        social.get("x", c).publish({"text": "hi"})


@responses.activate
def test_x_refresh_token_flow_used_when_configured():
    c = {"access_token": "old", "refresh_token": "RT", "client_id": "CID", "client_secret": "CS"}
    responses.post("https://api.x.com/2/oauth2/token", json={"access_token": "NEW"})
    responses.post("https://api.x.com/2/tweets", json={"data": {"id": "9"}})
    social.get("x", c).publish({"text": "hi"})
    assert responses.calls[-1].request.headers["Authorization"] == "Bearer NEW"


# -------------------------------------------------------------- LinkedIn
@responses.activate
def test_linkedin_text_post_headers_and_id():
    c = {"access_token": "LTOK", "author_urn": "urn:li:person:abc"}
    responses.get("https://api.linkedin.com/v2/userinfo", json={"name": "Ada"})
    assert social.get("linkedin", c).test_connection()["account"] == "Ada"
    responses.post("https://api.linkedin.com/rest/posts", status=201, headers={"x-restli-id": "urn:li:share:1"})
    out = social.get("linkedin", c).publish({"text": "post"})
    assert out == {"id": "urn:li:share:1"}
    req = responses.calls[-1].request
    assert req.headers["LinkedIn-Version"] and req.headers["X-Restli-Protocol-Version"] == "2.0.0"
    b = body_of(responses.calls[-1])
    assert b["author"] == "urn:li:person:abc" and b["commentary"] == "post" and b["lifecycleState"] == "PUBLISHED"


@responses.activate
def test_linkedin_image_upload_flow():
    c = {"access_token": "LTOK", "author_urn": "urn:li:person:abc"}
    responses.get("https://i/p.jpg", body=b"IMG")
    responses.post("https://api.linkedin.com/rest/images?action=initializeUpload",
                   json={"value": {"uploadUrl": "https://upload.li/x", "image": "urn:li:image:9"}})
    responses.put("https://upload.li/x", status=201)
    responses.post("https://api.linkedin.com/rest/posts", status=201, headers={"x-restli-id": "urn:li:share:2"})
    social.get("linkedin", c).publish({"text": "pic", "media_url": "https://i/p.jpg"})
    assert body_of(responses.calls[-1])["content"]["media"]["id"] == "urn:li:image:9"


# --------------------------------------------------------------- YouTube
@responses.activate
def test_youtube_resumable_upload_flow():
    c = {"client_id": "a", "client_secret": "b", "refresh_token": "rt"}
    responses.post("https://oauth2.googleapis.com/token", json={"access_token": "YT"})
    responses.get("https://www.googleapis.com/youtube/v3/channels", json={"items": [{"snippet": {"title": "Chan"}}]})
    assert social.get("youtube", c).test_connection()["account"] == "Chan"
    responses.get("https://v/video.mp4", body=b"VIDEOBYTES", headers={"Content-Type": "video/mp4"})
    responses.post("https://www.googleapis.com/upload/youtube/v3/videos", headers={"Location": "https://upload.google/session1"})
    responses.put("https://upload.google/session1", json={"id": "vid123"})
    out = social.get("youtube", c).publish({"title": "T", "text": "desc", "media_url": "https://v/video.mp4"})
    assert out == {"id": "vid123", "url": "https://youtu.be/vid123"}
    init = [x for x in responses.calls if "uploadType=resumable" in x.request.url][0]
    meta = json.loads(init.request.body)
    assert meta["snippet"]["title"] == "T" and meta["status"]["privacyStatus"] == "private"   # private by default
    with pytest.raises(http.PermanentError):
        social.get("youtube", c).publish({"title": "no media"})


# ---------------------------------------------------------- shared / API
def test_formatting_limits_per_platform():
    assert len(format_text("x", "a" * 500)["text"]) == 280
    assert format_text("x", "a" * 500)["text"].endswith("\u2026")
    assert len(format_text("threads", "b" * 900)["text"]) == 500
    assert len(format_text("reddit", "t" * 400, "T" * 400)["title"]) == 300
    assert format_text("pinterest", "x", "y" * 200)["title"].endswith("\u2026")
    assert format_text("linkedin", "  padded  ")["text"] == "padded"


def test_registry_has_all_eight_with_documented_permissions():
    spec = social.spec()
    assert set(spec) == {"reddit", "facebook", "instagram", "youtube", "x", "linkedin", "threads", "pinterest"}
    assert all(v["permissions"] and v["required"] for v in spec.values())


def test_credential_validation_reports_missing_fields():
    assert social.REGISTRY["facebook"].validate({"page_id": "1"}) == ["page_access_token"]
    assert social.REGISTRY["x"].validate({"access_token": "t"}) == []


def test_api_social_account_masking_update_and_validation(client):
    r = client.post("/api/settings/social", json={"platform": "facebook", "name": "Main",
                                                   "secrets": {"page_id": "123456789012", "page_access_token": "EAAB-very-secret-token-0042"}})
    assert r.status_code == 200
    a = r.json()
    blob = json.dumps(a) + json.dumps(client.get("/api/settings/social").json())
    assert "EAAB-very-secret" not in blob and a["secrets_masked"]["page_access_token"].endswith("0042")
    assert client.post("/api/settings/social", json={"platform": "facebook", "secrets": {"page_id": "1"}}).status_code == 400
    assert client.post("/api/settings/social", json={"platform": "myspace", "secrets": {}}).status_code == 400
    u = client.put(f"/api/settings/social/{a['id']}", json={"platform": "facebook", "name": "Renamed", "secrets": {"page_id": "999"}}).json()
    assert u["label"] == "Renamed" and u["secrets_masked"]["page_access_token"].endswith("0042")   # old token kept
    assert client.delete(f"/api/settings/social/{a['id']}").status_code == 200


@responses.activate
def test_api_social_connection_test_reports_failure(client):
    a = client.post("/api/settings/social", json={"platform": "pinterest", "secrets": {"access_token": "BAD-token-12345", "board_id": "1"}}).json()
    responses.get("https://api.pinterest.com/v5/user_account", status=401, body="invalid BAD-token-12345")
    out = client.post(f"/api/settings/social/{a['id']}/test").json()
    assert out["ok"] is False and "BAD-token-12345" not in out["message"]


@responses.activate
def test_end_to_end_job_publishes_via_worker_with_formatting(client, db):
    a = client.post("/api/settings/social", json={"platform": "x", "secrets": {"access_token": "XTOK-123456789"}}).json()
    responses.post("https://api.x.com/2/tweets", json={"data": {"id": "42"}})
    jid = client.post("/api/jobs", json={"task": "social_publish", "payload": {"account_id": a["id"], "text": "z" * 400}}).json()["id"]
    w = jobs.Worker(poll=0.02, with_scheduler=False)
    w.start()
    try:
        end = time.time() + 10
        while time.time() < end:
            j = client.get(f"/api/jobs/{jid}").json()
            if j["status"] in ("completed", "failed"):
                break
            time.sleep(0.05)
    finally:
        w.stop()
    assert j["status"] == "completed" and j["result"]["id"] == "42"
    assert len(json.loads(responses.calls[-1].request.body)["text"]) == 280
    assert "XTOK-123456789" not in json.dumps(j)


@responses.activate
def test_end_to_end_rate_limited_publish_is_requeued_not_lost(client, db):
    a = client.post("/api/settings/social", json={"platform": "x", "secrets": {"access_token": "XTOK-123456789"}}).json()
    responses.post("https://api.x.com/2/tweets", status=429, headers={"Retry-After": "600"}, body="limit")
    jid = client.post("/api/jobs", json={"task": "social_publish", "payload": {"account_id": a["id"], "text": "hi"}}).json()["id"]
    w = jobs.Worker(poll=0.02, with_scheduler=False)
    w.start()
    try:
        end = time.time() + 10
        while time.time() < end:
            j = client.get(f"/api/jobs/{jid}").json()
            if j["attempts"] >= 1 and j["status"] == "queued":
                break
            time.sleep(0.05)
    finally:
        w.stop()
    assert j["status"] == "queued" and "429" in j["error"]
    assert j["run_after"] > j["created_at"]


def test_job_requires_existing_account(client):
    assert client.post("/api/jobs", json={"task": "social_publish", "payload": {"account_id": "nope"}}).status_code == 400
    assert client.post("/api/jobs", json={"task": "evil"}).status_code == 400
