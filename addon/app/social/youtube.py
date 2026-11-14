import requests

from .. import http
from .base import Platform

YT = "https://www.googleapis.com/youtube/v3"
UP = "https://www.googleapis.com/upload/youtube/v3/videos"


class YouTube(Platform):
    name = "youtube"
    required = ("client_id", "client_secret", "refresh_token")
    permissions = ("OAuth scope https://www.googleapis.com/auth/youtube.upload (+ youtube.readonly for the test). "
                   "Each upload costs ~1600 quota units of the default 10,000/day; videos from unverified API projects are locked private.")
    media = "video upload via resumable protocol (media_url must be a downloadable video file)"

    def _token(self) -> str:
        r = self.call("POST", "https://oauth2.googleapis.com/token",
                      data={"client_id": self.creds["client_id"], "client_secret": self.creds["client_secret"],
                            "refresh_token": self.creds["refresh_token"], "grant_type": "refresh_token"})
        return r.json()["access_token"]

    def test_connection(self) -> dict:
        r = self.call("GET", f"{YT}/channels", params={"part": "snippet", "mine": "true"},
                      headers={"Authorization": f"Bearer {self._token()}"})
        items = r.json().get("items", [])
        return {"ok": True, "account": items[0]["snippet"]["title"] if items else None}

    def publish(self, payload: dict) -> dict:
        media = payload.get("media_url")
        if not media:
            raise http.PermanentError("YouTube requires payload.media_url (video file URL)")
        title = (payload.get("title") or payload.get("text", "")[:100] or "Untitled")[:100]
        meta = {"snippet": {"title": title, "description": payload.get("text", "")[:5000],
                            "tags": payload.get("tags", [])},
                "status": {"privacyStatus": payload.get("privacy", "private"), "selfDeclaredMadeForKids": False}}
        tok = self._token()
        try:
            src = requests.get(media, stream=True, timeout=60)
            src.raise_for_status()
        except requests.RequestException as exc:
            raise http.RetryableError(f"Could not download media: {exc}")
        length = src.headers.get("Content-Length")
        init = self.call("POST", UP, params={"uploadType": "resumable", "part": "snippet,status"},
                         headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json; charset=UTF-8",
                                  "X-Upload-Content-Type": src.headers.get("Content-Type", "video/*"),
                                  **({"X-Upload-Content-Length": length} if length else {})},
                         json=meta)
        location = init.headers.get("Location")
        if not location:
            raise http.PermanentError("YouTube did not return a resumable upload URL")
        up = self.call("PUT", location, data=src.iter_content(chunk_size=1024 * 1024),
                       headers={"Authorization": f"Bearer {tok}", **({"Content-Length": length} if length else {})},
                       timeout=600)
        vid = up.json().get("id")
        return {"id": vid, "url": f"https://youtu.be/{vid}"}
