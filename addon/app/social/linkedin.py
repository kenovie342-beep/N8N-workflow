from .. import http
from .base import Platform

L = "https://api.linkedin.com"


class LinkedIn(Platform):
    name = "linkedin"
    required = ("access_token", "author_urn")
    optional = ("api_version",)
    permissions = "OAuth scopes: w_member_social (personal) or w_organization_social (page), plus openid/profile for the connection test."
    media = "single image (initializeUpload + binary PUT)"
    notes = "Uses the versioned /rest/posts API. Set api_version (YYYYMM) in config if LinkedIn sunsets the default."

    def _h(self) -> dict:
        return {"Authorization": f"Bearer {self.creds['access_token']}",
                "LinkedIn-Version": self.creds.get("api_version") or self.config.get("api_version") or "202510",
                "X-Restli-Protocol-Version": "2.0.0"}

    def test_connection(self) -> dict:
        r = self.call("GET", f"{L}/v2/userinfo", headers={"Authorization": f"Bearer {self.creds['access_token']}"})
        return {"ok": True, "account": r.json().get("name")}

    def _upload_image(self, url: str) -> str:
        img = self.call("GET", url, timeout=60)
        init = self.call("POST", f"{L}/rest/images?action=initializeUpload", headers=self._h(),
                         json={"initializeUploadRequest": {"owner": self.creds["author_urn"]}}).json()["value"]
        self.call("PUT", init["uploadUrl"], data=img.content,
                  headers={"Authorization": f"Bearer {self.creds['access_token']}"})
        return init["image"]

    def publish(self, payload: dict) -> dict:
        text = payload.get("text", "")
        if not text:
            raise http.PermanentError("payload.text is required")
        body = {"author": self.creds["author_urn"], "commentary": text, "visibility": "PUBLIC",
                "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
                "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
        if payload.get("media_url"):
            body["content"] = {"media": {"id": self._upload_image(payload["media_url"]), "altText": payload.get("alt_text", "")}}
        r = self.call("POST", f"{L}/rest/posts", headers={**self._h(), "Content-Type": "application/json"}, json=body)
        return {"id": r.headers.get("x-restli-id") or r.headers.get("x-linkedin-id")}
