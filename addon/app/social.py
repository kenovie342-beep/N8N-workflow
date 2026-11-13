import requests
from .security import decrypt

# Credentials and endpoint settings are stored encrypted. The publishing method deliberately
# exposes only official API calls; each platform has different app-review and permission rules.
PLATFORMS = {
 "reddit": {"fields":["client_id","client_secret","access_token","user_agent"],"capability":"submit"},
 "facebook": {"fields":["page_access_token","page_id"],"capability":"feed_publish"},
 "instagram": {"fields":["access_token","ig_user_id"],"capability":"media_publish"},
 "youtube": {"fields":["access_token","channel_id"],"capability":"videos_insert"},
 "x": {"fields":["bearer_token","access_token","access_token_secret"],"capability":"tweets_create"},
 "linkedin": {"fields":["access_token","author_urn"],"capability":"ugc_posts"},
 "threads": {"fields":["access_token","user_id"],"capability":"threads_publish"},
 "pinterest": {"fields":["access_token","board_id"],"capability":"pins_create"}
}

def publish(platform: str, config: dict, secret_enc: str, payload: dict):
    if platform not in PLATFORMS: raise ValueError("Unsupported platform")
    secret = __import__("json").loads(decrypt(secret_enc)) if secret_enc else {}
    text = payload.get("text","")
    if not text and platform not in ("instagram","youtube","pinterest"):
        raise ValueError("payload.text is required")
    if platform == "facebook":
        url=f"https://graph.facebook.com/v21.0/{secret['page_id']}/feed"
        r=requests.post(url,data={"message":text,"access_token":secret["page_access_token"]},timeout=30)
    elif platform == "instagram":
        # Instagram publishing is a two-step container creation + publish operation.
        image_url=payload.get("image_url")
        if not image_url: raise ValueError("Instagram requires a publicly reachable image_url")
        base=f"https://graph.facebook.com/v21.0/{secret['ig_user_id']}"
        c=requests.post(base+"/media",data={"image_url":image_url,"caption":text,"access_token":secret["access_token"]},timeout=30); c.raise_for_status()
        r=requests.post(base+"/media_publish",data={"creation_id":c.json()["id"],"access_token":secret["access_token"]},timeout=30)
    elif platform == "threads":
        base=f"https://graph.threads.net/v1.0/{secret['user_id']}"
        c=requests.post(base+"/threads",data={"media_type":"TEXT","text":text,"access_token":secret["access_token"]},timeout=30); c.raise_for_status()
        r=requests.post(base+"/threads_publish",data={"creation_id":c.json()["id"],"access_token":secret["access_token"]},timeout=30)
    elif platform == "linkedin":
        r=requests.post("https://api.linkedin.com/v2/ugcPosts",headers={"Authorization":"Bearer "+secret["access_token"],"X-Restli-Protocol-Version":"2.0.0"},
          json={"author":secret["author_urn"],"lifecycleState":"PUBLISHED","specificContent":{"com.linkedin.ugc.ShareContent":{"shareCommentary":{"text":text},"shareMediaCategory":"NONE"}},"visibility":{"com.linkedin.ugc.MemberNetworkVisibility":"PUBLIC"}},timeout=30)
    elif platform == "x":
        r=requests.post("https://api.x.com/2/tweets",headers={"Authorization":"Bearer "+secret["bearer_token"],"Content-Type":"application/json"},json={"text":text},timeout=30)
    elif platform == "reddit":
        r=requests.post("https://oauth.reddit.com/api/submit",headers={"Authorization":"Bearer "+secret["access_token"],"User-Agent":secret.get("user_agent","AutomationSuite/1.0")},data={"sr":payload["subreddit"],"kind":"self","title":payload.get("title",text[:100]),"text":text},timeout=30)
    elif platform == "pinterest":
        r=requests.post("https://api.pinterest.com/v5/pins",headers={"Authorization":"Bearer "+secret["access_token"]},json={"board_id":secret["board_id"],"media_source":{"source_type":"image_url","url":payload["image_url"]},"description":text},timeout=30)
    elif platform == "youtube":
        raise ValueError("YouTube video upload requires a resumable media upload; use the YouTube upload adapter extension. This account credential is stored, but this base adapter does not upload video bytes.")
    r.raise_for_status()
    return {"status":"published","platform":platform,"response":r.json()}
