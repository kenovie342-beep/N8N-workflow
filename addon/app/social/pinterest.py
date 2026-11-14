from .. import http
from .base import Platform

P = "https://api.pinterest.com/v5"


class Pinterest(Platform):
    name = "pinterest"
    required = ("access_token", "board_id")
    permissions = "OAuth scopes: boards:read, pins:read, pins:write (trial access is sandbox-only until upgraded)."
    media = "image URL pins (video pins need the separate media-upload flow, not implemented)"

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.creds['access_token']}"}

    def test_connection(self) -> dict:
        r = self.call("GET", f"{P}/user_account", headers=self._h())
        return {"ok": True, "account": r.json().get("username")}

    def publish(self, payload: dict) -> dict:
        media = payload.get("media_url") or payload.get("image_url")
        if not media:
            raise http.PermanentError("Pinterest requires payload.media_url (image URL)")
        body = {"board_id": self.creds["board_id"],
                "media_source": {"source_type": "image_url", "url": media},
                "description": payload.get("text", "")}
        if payload.get("title"):
            body["title"] = payload["title"]
        if payload.get("link"):
            body["link"] = payload["link"]
        r = self.call("POST", f"{P}/pins", headers=self._h(), json=body)
        return {"id": r.json().get("id")}
