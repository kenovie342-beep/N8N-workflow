import time

from .. import http
from .base import Platform

T = "https://graph.threads.net/v1.0"


class Threads(Platform):
    name = "threads"
    required = ("user_id", "access_token")
    permissions = "Threads API: threads_basic, threads_content_publish."
    media = "image_url or video_url; text-only supported"

    def test_connection(self) -> dict:
        r = self.call("GET", f"{T}/me", params={"fields": "id,username", "access_token": self.creds["access_token"]})
        return {"ok": True, "account": r.json().get("username")}

    def publish(self, payload: dict) -> dict:
        uid, tok = self.creds["user_id"], self.creds["access_token"]
        media, text = payload.get("media_url"), payload.get("text", "")
        data = {"access_token": tok, "text": text}
        if media and payload.get("media_type") == "video":
            data.update(media_type="VIDEO", video_url=media)
        elif media:
            data.update(media_type="IMAGE", image_url=media)
        else:
            if not text:
                raise http.PermanentError("payload.text is required")
            data["media_type"] = "TEXT"
        c = self.call("POST", f"{T}/{uid}/threads", data=data).json()
        if media:  # media containers need processing time before publish
            delay = float(self.config.get("poll_seconds", 5))
            for _ in range(30):
                st = self.call("GET", f"{T}/{c['id']}", params={"fields": "status", "access_token": tok}).json().get("status")
                if st == "FINISHED":
                    break
                if st in ("ERROR", "EXPIRED"):
                    raise http.PermanentError(f"Threads media processing failed: {st}")
                time.sleep(delay)
        p = self.call("POST", f"{T}/{uid}/threads_publish", data={"creation_id": c["id"], "access_token": tok}).json()
        return {"id": p.get("id")}
