from .. import http
from .base import Platform

G = "https://graph.facebook.com/v21.0"


class Facebook(Platform):
    name = "facebook"
    required = ("page_id", "page_access_token")
    permissions = "Page access token with pages_manage_posts, pages_read_engagement (app review required for non-admin use)."
    media = "photo URL, video URL; native scheduling via scheduled_publish_time (10 min - 30 days ahead)"

    def test_connection(self) -> dict:
        r = self.call("GET", f"{G}/{self.creds['page_id']}",
                      params={"fields": "id,name", "access_token": self.creds["page_access_token"]})
        return {"ok": True, "account": r.json().get("name")}

    def publish(self, payload: dict) -> dict:
        pid, tok = self.creds["page_id"], self.creds["page_access_token"]
        text, media = payload.get("text", ""), payload.get("media_url")
        data = {"access_token": tok}
        if payload.get("scheduled_publish_time"):
            data.update(published="false", scheduled_publish_time=int(payload["scheduled_publish_time"]))
        if media and payload.get("media_type") == "video":
            data.update(file_url=media, description=text)
            r = self.call("POST", f"{G}/{pid}/videos", data=data)
        elif media:
            data.update(url=media, caption=text)
            r = self.call("POST", f"{G}/{pid}/photos", data=data)
        else:
            if not text:
                raise http.PermanentError("payload.text is required")
            data["message"] = text
            if payload.get("link"):
                data["link"] = payload["link"]
            r = self.call("POST", f"{G}/{pid}/feed", data=data)
        j = r.json()
        return {"id": j.get("id") or j.get("post_id")}
