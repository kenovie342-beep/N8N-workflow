import time

from .. import http
from .base import Platform

G = "https://graph.facebook.com/v21.0"


class Instagram(Platform):
    name = "instagram"
    required = ("ig_user_id", "access_token")
    permissions = "Instagram Business/Creator account linked to a Page; instagram_basic, instagram_content_publish."
    media = "image_url or video_url (REELS) - media must be publicly reachable; text-only posts are not allowed"

    def test_connection(self) -> dict:
        r = self.call("GET", f"{G}/{self.creds['ig_user_id']}",
                      params={"fields": "id,username", "access_token": self.creds["access_token"]})
        return {"ok": True, "account": r.json().get("username")}

    def _wait_ready(self, container: str, tries: int = 30, delay: float = 5.0):
        for _ in range(tries):
            r = self.call("GET", f"{G}/{container}",
                          params={"fields": "status_code", "access_token": self.creds["access_token"]})
            st = r.json().get("status_code")
            if st == "FINISHED":
                return
            if st in ("ERROR", "EXPIRED"):
                raise http.PermanentError(f"Instagram media processing failed: {st}")
            time.sleep(delay)
        raise http.RetryableError("Instagram media still processing", 30)

    def publish(self, payload: dict) -> dict:
        uid, tok = self.creds["ig_user_id"], self.creds["access_token"]
        media = payload.get("media_url") or payload.get("image_url")
        if not media:
            raise http.PermanentError("Instagram requires payload.media_url (public image or video URL)")
        data = {"caption": payload.get("text", ""), "access_token": tok}
        is_video = payload.get("media_type") == "video"
        if is_video:
            data.update(media_type="REELS", video_url=media)
        else:
            data["image_url"] = media
        c = self.call("POST", f"{G}/{uid}/media", data=data).json()
        if is_video:
            self._wait_ready(c["id"], delay=float(self.config.get("poll_seconds", 5)))
        p = self.call("POST", f"{G}/{uid}/media_publish", data={"creation_id": c["id"], "access_token": tok}).json()
        return {"id": p.get("id")}
