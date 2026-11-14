from .. import http
from .base import Platform

API = "https://api.x.com/2"


class X(Platform):
    name = "x"
    required = ("access_token",)
    optional = ("client_id", "client_secret", "refresh_token")
    permissions = ("OAuth 2.0 user-context token (PKCE) with tweet.read, tweet.write, users.read, media.write, offline.access. "
                   "An app-only bearer token CANNOT post. X API access is paid/credit-based.")
    media = "images via media upload; video not implemented"

    def _token(self) -> str:
        if self.creds.get("refresh_token") and self.creds.get("client_id"):
            kw = {"auth": (self.creds["client_id"], self.creds["client_secret"])} if self.creds.get("client_secret") else {}
            r = self.call("POST", f"{API}/oauth2/token", **kw,
                          data={"grant_type": "refresh_token", "refresh_token": self.creds["refresh_token"],
                                "client_id": self.creds["client_id"]})
            return r.json()["access_token"]
        return self.creds["access_token"]

    def test_connection(self) -> dict:
        r = self.call("GET", f"{API}/users/me", headers={"Authorization": f"Bearer {self._token()}"})
        return {"ok": True, "account": r.json().get("data", {}).get("username")}

    def publish(self, payload: dict) -> dict:
        text = payload.get("text", "")
        if not text:
            raise http.PermanentError("payload.text is required")
        h = {"Authorization": f"Bearer {self._token()}"}
        body = {"text": text}
        if payload.get("media_url"):
            img = self.call("GET", payload["media_url"], timeout=60)
            up = self.call("POST", f"{API}/media/upload", headers=h,
                           data={"media_category": "tweet_image"},
                           files={"media": ("image", img.content, img.headers.get("Content-Type", "image/jpeg"))})
            mid = up.json().get("data", {}).get("id") or up.json().get("media_id_string")
            body["media"] = {"media_ids": [str(mid)]}
        r = self.call("POST", f"{API}/tweets", headers={**h, "Content-Type": "application/json"}, json=body)
        d = r.json().get("data", {})
        return {"id": d.get("id"), "url": f"https://x.com/i/web/status/{d.get('id')}"}
