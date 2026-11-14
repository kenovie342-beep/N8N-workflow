from .. import http
from .base import Platform

UA = "n8n-automation-suite/2.0"


class Reddit(Platform):
    name = "reddit"
    required = ("client_id", "client_secret")
    optional = ("refresh_token", "access_token", "user_agent")
    permissions = "OAuth2 scopes: identity, submit (refresh_token flow). Script/installed app from reddit.com/prefs/apps."
    media = "link posts only (image/video upload not implemented)"

    def _headers(self) -> dict:
        ua = self.creds.get("user_agent") or UA
        token = self.creds.get("access_token")
        if self.creds.get("refresh_token"):
            r = self.call("POST", "https://www.reddit.com/api/v1/access_token",
                          auth=(self.creds["client_id"], self.creds["client_secret"]),
                          data={"grant_type": "refresh_token", "refresh_token": self.creds["refresh_token"]},
                          headers={"User-Agent": ua})
            token = r.json().get("access_token")
        if not token:
            raise http.PermanentError("Reddit needs a refresh_token or access_token")
        return {"Authorization": f"Bearer {token}", "User-Agent": ua}

    def test_connection(self) -> dict:
        r = self.call("GET", "https://oauth.reddit.com/api/v1/me", headers=self._headers())
        return {"ok": True, "account": r.json().get("name")}

    def publish(self, payload: dict) -> dict:
        sub = payload.get("subreddit")
        if not sub:
            raise http.PermanentError("payload.subreddit is required for Reddit")
        title = payload.get("title") or (payload.get("text", "")[:100])
        if not title:
            raise http.PermanentError("Reddit requires a title")
        data = {"api_type": "json", "sr": sub, "title": title}
        if payload.get("media_url"):
            data.update(kind="link", url=payload["media_url"])
        else:
            data.update(kind="self", text=payload.get("text", ""))
        r = self.call("POST", "https://oauth.reddit.com/api/submit", headers=self._headers(), data=data)
        body = r.json().get("json", {})
        errs = body.get("errors") or []
        if errs:
            code = str(errs[0][0]) if errs[0] else "ERROR"
            msg = f"Reddit rejected the post: {errs[0]}"
            if code in ("RATELIMIT", "TOO_MANY_REQUESTS"):
                raise http.RetryableError(msg, 60)
            raise http.PermanentError(msg)
        d = body.get("data", {})
        return {"id": d.get("id") or d.get("name"), "url": d.get("url")}
