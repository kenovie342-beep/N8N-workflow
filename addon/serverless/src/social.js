// Eight social platform modules (official APIs). Each: required/optional credential fields, documented permissions,
// testConnection(creds, cfg, f) and publish(creds, cfg, payload, f). `f` carries {fetchFn, sleep}.
import { Permanent, UpstreamError, form, jsonBody, request } from "./http.js";
import { sleep as realSleep } from "./util.js";

export const LIMITS = { x: 280, threads: 500, linkedin: 3000, instagram: 2200, facebook: 63000, reddit: 40000, pinterest: 800, youtube: 5000 };
export const TITLE_LIMITS = { reddit: 300, pinterest: 100, youtube: 100 };

export function formatText(platform, text, title = "") {
  let t = String(text || "").trim().replace(/\n{3,}/g, "\n\n");
  const limit = LIMITS[platform] || 2000;
  if (t.length > limit) t = t.slice(0, limit - 1).trimEnd() + "\u2026";
  const out = { text: t }, tl = TITLE_LIMITS[platform];
  if (tl) { const ti = String(title || t.split("\n")[0]).trim(); out.title = ti.length <= tl ? ti : ti.slice(0, tl - 1).trimEnd() + "\u2026"; }
  return out;
}

const mk = (creds, f) => (method, url, opts = {}) => request(method, url, { secrets: Object.values(creds).map(String), fetchFn: f.fetchFn, ...opts });
const bearer = (t) => ({ authorization: `Bearer ${t}` });
const need = (cond, msg) => { if (!cond) throw Permanent(msg); };

// ------------------------------------------------------------------ Reddit
const reddit = {
  required: ["client_id", "client_secret"], optional: ["refresh_token", "access_token", "user_agent"],
  permissions: "OAuth2 scopes: identity, submit (refresh_token flow). Script/installed app from reddit.com/prefs/apps.", media: "link posts only (image/video upload not implemented)",
  async headers(c, call) {
    const ua = c.user_agent || "n8n-automation-suite/3.0"; let token = c.access_token;
    if (c.refresh_token) {
      const r = await call("POST", "https://www.reddit.com/api/v1/access_token", { headers: { authorization: "Basic " + btoa(`${c.client_id}:${c.client_secret}`), "user-agent": ua, "content-type": "application/x-www-form-urlencoded" },
        body: form({ grant_type: "refresh_token", refresh_token: c.refresh_token }) });
      token = (await r.json()).access_token;
    }
    need(token, "Reddit needs a refresh_token or access_token");
    return { ...bearer(token), "user-agent": ua };
  },
  async testConnection(c, cfg, f) { const call = mk(c, f); const r = await call("GET", "https://oauth.reddit.com/api/v1/me", { headers: await this.headers(c, call) }); return { ok: true, account: (await r.json()).name }; },
  async publish(c, cfg, p, f) {
    need(p.subreddit, "payload.subreddit is required for Reddit");
    const title = p.title || String(p.text || "").slice(0, 100); need(title, "Reddit requires a title");
    const call = mk(c, f), data = { api_type: "json", sr: p.subreddit, title, ...(p.media_url ? { kind: "link", url: p.media_url } : { kind: "self", text: p.text || "" }) };
    const r = await call("POST", "https://oauth.reddit.com/api/submit", { headers: { ...(await this.headers(c, call)), "content-type": "application/x-www-form-urlencoded" }, body: form(data) });
    const b = (await r.json()).json || {}, errs = b.errors || [];
    if (errs.length) { const msg = `Reddit rejected the post: ${JSON.stringify(errs[0])}`; throw ["RATELIMIT", "TOO_MANY_REQUESTS"].includes(String(errs[0][0])) ? new UpstreamError(msg, true, 60) : Permanent(msg); }
    return { id: b.data?.id || b.data?.name, url: b.data?.url };
  },
};

// ---------------------------------------------------------------- Facebook
const G = "https://graph.facebook.com/v21.0";
const facebook = {
  required: ["page_id", "page_access_token"], optional: [],
  permissions: "Page access token with pages_manage_posts, pages_read_engagement (app review needed beyond app admins).", media: "photo URL, video URL; native scheduling via scheduled_publish_time (10 min-30 days ahead)",
  async testConnection(c, cfg, f) { const r = await mk(c, f)("GET", `${G}/${c.page_id}?${form({ fields: "id,name", access_token: c.page_access_token })}`); return { ok: true, account: (await r.json()).name }; },
  async publish(c, cfg, p, f) {
    const call = mk(c, f), d = { access_token: c.page_access_token };
    if (p.scheduled_publish_time) Object.assign(d, { published: "false", scheduled_publish_time: parseInt(p.scheduled_publish_time, 10) });
    let path;
    if (p.media_url && p.media_type === "video") { Object.assign(d, { file_url: p.media_url, description: p.text || "" }); path = "videos"; }
    else if (p.media_url) { Object.assign(d, { url: p.media_url, caption: p.text || "" }); path = "photos"; }
    else { need(p.text, "payload.text is required"); d.message = p.text; if (p.link) d.link = p.link; path = "feed"; }
    const j = await (await call("POST", `${G}/${c.page_id}/${path}`, { headers: { "content-type": "application/x-www-form-urlencoded" }, body: form(d) })).json();
    return { id: j.id || j.post_id };
  },
};

// --------------------------------------------------------------- Instagram
const instagram = {
  required: ["ig_user_id", "access_token"], optional: [],
  permissions: "Instagram Business/Creator account linked to a Page; instagram_basic, instagram_content_publish.", media: "image_url or video_url (REELS), public URL; text-only posts are not allowed",
  async testConnection(c, cfg, f) { const r = await mk(c, f)("GET", `${G}/${c.ig_user_id}?${form({ fields: "id,username", access_token: c.access_token })}`); return { ok: true, account: (await r.json()).username }; },
  async publish(c, cfg, p, f) {
    const call = mk(c, f), media = p.media_url || p.image_url; need(media, "Instagram requires payload.media_url (public image or video URL)");
    const video = p.media_type === "video", fh = { "content-type": "application/x-www-form-urlencoded" };
    const cont = await (await call("POST", `${G}/${c.ig_user_id}/media`, { headers: fh, body: form({ caption: p.text || "", access_token: c.access_token, ...(video ? { media_type: "REELS", video_url: media } : { image_url: media }) }) })).json();
    if (video) {
      const delay = Number(cfg.poll_seconds ?? 5) * 1000; let ok = false;
      for (let i = 0; i < 30 && !ok; i++) {
        const st = (await (await call("GET", `${G}/${cont.id}?${form({ fields: "status_code", access_token: c.access_token })}`)).json()).status_code;
        if (st === "FINISHED") ok = true; else if (st === "ERROR" || st === "EXPIRED") throw Permanent(`Instagram media processing failed: ${st}`); else await f.sleep(delay);
      }
      if (!ok) throw new UpstreamError("Instagram media still processing", true, 30);
    }
    const pub = await (await call("POST", `${G}/${c.ig_user_id}/media_publish`, { headers: fh, body: form({ creation_id: cont.id, access_token: c.access_token }) })).json();
    return { id: pub.id };
  },
};

// ----------------------------------------------------------------- Threads
const T = "https://graph.threads.net/v1.0";
const threads = {
  required: ["user_id", "access_token"], optional: [], permissions: "Threads API: threads_basic, threads_content_publish.", media: "image_url or video_url; text-only supported",
  async testConnection(c, cfg, f) { const r = await mk(c, f)("GET", `${T}/me?${form({ fields: "id,username", access_token: c.access_token })}`); return { ok: true, account: (await r.json()).username }; },
  async publish(c, cfg, p, f) {
    const call = mk(c, f), fh = { "content-type": "application/x-www-form-urlencoded" }, d = { access_token: c.access_token, text: p.text || "" };
    if (p.media_url && p.media_type === "video") Object.assign(d, { media_type: "VIDEO", video_url: p.media_url });
    else if (p.media_url) Object.assign(d, { media_type: "IMAGE", image_url: p.media_url });
    else { need(p.text, "payload.text is required"); d.media_type = "TEXT"; }
    const cont = await (await call("POST", `${T}/${c.user_id}/threads`, { headers: fh, body: form(d) })).json();
    if (p.media_url) {
      const delay = Number(cfg.poll_seconds ?? 5) * 1000;
      for (let i = 0; i < 30; i++) {
        const st = (await (await call("GET", `${T}/${cont.id}?${form({ fields: "status", access_token: c.access_token })}`)).json()).status;
        if (st === "FINISHED") break; if (st === "ERROR" || st === "EXPIRED") throw Permanent(`Threads media processing failed: ${st}`); await f.sleep(delay);
      }
    }
    return { id: (await (await call("POST", `${T}/${c.user_id}/threads_publish`, { headers: fh, body: form({ creation_id: cont.id, access_token: c.access_token }) })).json()).id };
  },
};

// --------------------------------------------------------------- Pinterest
const P = "https://api.pinterest.com/v5";
const pinterest = {
  required: ["access_token", "board_id"], optional: [], permissions: "OAuth scopes: boards:read, pins:read, pins:write (trial access is sandbox-only until upgraded).", media: "image URL pins (video pins not implemented)",
  async testConnection(c, cfg, f) { const r = await mk(c, f)("GET", `${P}/user_account`, { headers: bearer(c.access_token) }); return { ok: true, account: (await r.json()).username }; },
  async publish(c, cfg, p, f) {
    const media = p.media_url || p.image_url; need(media, "Pinterest requires payload.media_url (image URL)");
    const body = { board_id: c.board_id, media_source: { source_type: "image_url", url: media }, description: p.text || "", ...(p.title ? { title: p.title } : {}), ...(p.link ? { link: p.link } : {}) };
    const r = await mk(c, f)("POST", `${P}/pins`, { headers: { ...bearer(c.access_token), "content-type": "application/json" }, body: jsonBody(body) });
    return { id: (await r.json()).id };
  },
};

// ----------------------------------------------------------------------- X
const XA = "https://api.x.com/2";
const x = {
  required: ["access_token"], optional: ["client_id", "client_secret", "refresh_token"],
  permissions: "OAuth 2.0 user-context token (PKCE) with tweet.read, tweet.write, users.read, media.write, offline.access. An app-only bearer token CANNOT post. X API access is paid/credit-based.", media: "images via media upload; video not implemented",
  async token(c, call) {
    if (c.refresh_token && c.client_id) {
      const r = await call("POST", `${XA}/oauth2/token`, { headers: { "content-type": "application/x-www-form-urlencoded", ...(c.client_secret ? { authorization: "Basic " + btoa(`${c.client_id}:${c.client_secret}`) } : {}) },
        body: form({ grant_type: "refresh_token", refresh_token: c.refresh_token, client_id: c.client_id }) });
      return (await r.json()).access_token;
    }
    return c.access_token;
  },
  async testConnection(c, cfg, f) { const call = mk(c, f); const r = await call("GET", `${XA}/users/me`, { headers: bearer(await this.token(c, call)) }); return { ok: true, account: (await r.json()).data?.username }; },
  async publish(c, cfg, p, f) {
    need(p.text, "payload.text is required");
    const call = mk(c, f), h = bearer(await this.token(c, call)), body = { text: p.text };
    if (p.media_url) {
      const img = await call("GET", p.media_url), fd = new FormData();
      fd.append("media_category", "tweet_image"); fd.append("media", new Blob([await img.arrayBuffer()], { type: img.headers.get("content-type") || "image/jpeg" }), "image");
      const up = await (await call("POST", `${XA}/media/upload`, { headers: h, body: fd })).json();
      body.media = { media_ids: [String(up.data?.id || up.media_id_string)] };
    }
    const d = (await (await call("POST", `${XA}/tweets`, { headers: { ...h, "content-type": "application/json" }, body: jsonBody(body) })).json()).data || {};
    return { id: d.id, url: `https://x.com/i/web/status/${d.id}` };
  },
};

// ---------------------------------------------------------------- LinkedIn
const L = "https://api.linkedin.com";
const linkedin = {
  required: ["access_token", "author_urn"], optional: ["api_version"],
  permissions: "OAuth scopes: w_member_social (personal) or w_organization_social (page), plus openid/profile for the connection test.", media: "single image (initializeUpload + binary PUT)",
  notes: "Uses the versioned /rest/posts API; set api_version (YYYYMM) per account if LinkedIn sunsets the default.",
  h: (c, cfg) => ({ ...bearer(c.access_token), "linkedin-version": c.api_version || cfg.api_version || "202510", "x-restli-protocol-version": "2.0.0" }),
  async testConnection(c, cfg, f) { const r = await mk(c, f)("GET", `${L}/v2/userinfo`, { headers: bearer(c.access_token) }); return { ok: true, account: (await r.json()).name }; },
  async publish(c, cfg, p, f) {
    need(p.text, "payload.text is required");
    const call = mk(c, f), h = this.h(c, cfg);
    const body = { author: c.author_urn, commentary: p.text, visibility: "PUBLIC", distribution: { feedDistribution: "MAIN_FEED", targetEntities: [], thirdPartyDistributionChannels: [] }, lifecycleState: "PUBLISHED", isReshareDisabledByAuthor: false };
    if (p.media_url) {
      const img = await call("GET", p.media_url);
      const init = (await (await call("POST", `${L}/rest/images?action=initializeUpload`, { headers: { ...h, "content-type": "application/json" }, body: jsonBody({ initializeUploadRequest: { owner: c.author_urn } }) })).json()).value;
      await call("PUT", init.uploadUrl, { headers: bearer(c.access_token), body: await img.arrayBuffer() });
      body.content = { media: { id: init.image, altText: p.alt_text || "" } };
    }
    const r = await call("POST", `${L}/rest/posts`, { headers: { ...h, "content-type": "application/json" }, body: jsonBody(body) });
    return { id: r.headers.get("x-restli-id") || r.headers.get("x-linkedin-id") };
  },
};

// ----------------------------------------------------------------- YouTube
const YT = "https://www.googleapis.com/youtube/v3", UP = "https://www.googleapis.com/upload/youtube/v3/videos";
const MAX_VIDEO_BYTES = 60 * 1024 * 1024;                                       // Worker memory is limited; larger videos need the GitHub runner
const youtube = {
  required: ["client_id", "client_secret", "refresh_token"], optional: [],
  permissions: "OAuth scope https://www.googleapis.com/auth/youtube.upload (+ youtube.readonly for the test). ~1600 quota units per upload of the 10,000/day default; videos from unverified API projects are locked private.",
  media: "video upload via resumable protocol (media_url must be a downloadable video file, up to ~60 MB in the Worker)",
  async token(c, call) { const r = await call("POST", "https://oauth2.googleapis.com/token", { headers: { "content-type": "application/x-www-form-urlencoded" }, body: form({ client_id: c.client_id, client_secret: c.client_secret, refresh_token: c.refresh_token, grant_type: "refresh_token" }) }); return (await r.json()).access_token; },
  async testConnection(c, cfg, f) { const call = mk(c, f); const r = await call("GET", `${YT}/channels?part=snippet&mine=true`, { headers: bearer(await this.token(c, call)) }); const it = (await r.json()).items || []; return { ok: true, account: it[0]?.snippet?.title ?? null }; },
  async publish(c, cfg, p, f) {
    need(p.media_url, "YouTube requires payload.media_url (video file URL)");
    const call = mk(c, f), tok = await this.token(c, call);
    const src = await call("GET", p.media_url, { headers: {} }), bytes = await src.arrayBuffer();
    need(bytes.byteLength <= MAX_VIDEO_BYTES, "Video is too large for the Worker (limit ~60 MB); use a smaller file");
    const meta = { snippet: { title: String(p.title || String(p.text || "").slice(0, 100) || "Untitled").slice(0, 100), description: String(p.text || "").slice(0, 5000), tags: p.tags || [] }, status: { privacyStatus: p.privacy || "private", selfDeclaredMadeForKids: false } };
    const init = await call("POST", `${UP}?uploadType=resumable&part=snippet,status`, { headers: { ...bearer(tok), "content-type": "application/json; charset=UTF-8", "x-upload-content-type": src.headers.get("content-type") || "video/*", "x-upload-content-length": String(bytes.byteLength) }, body: jsonBody(meta) });
    const loc = init.headers.get("location"); need(loc, "YouTube did not return a resumable upload URL");
    const vid = (await (await call("PUT", loc, { headers: bearer(tok), body: bytes })).json()).id;
    return { id: vid, url: `https://youtu.be/${vid}` };
  },
};

export const REGISTRY = { reddit, facebook, instagram, youtube, x, linkedin, threads, pinterest };
export const missingFields = (platform, creds) => REGISTRY[platform].required.filter((k) => !creds[k]);
export const spec = () => Object.fromEntries(Object.entries(REGISTRY).map(([n, m]) => [n, { required: m.required, optional: m.optional, permissions: m.permissions, media: m.media, notes: m.notes || "", text_limit: LIMITS[n] }]));
export const defaultDeps = () => ({ fetchFn: (...a) => fetch(...a), sleep: realSleep });
export async function testAccount(platform, creds, cfg = {}, deps = defaultDeps()) {
  try { return await REGISTRY[platform].testConnection(creds, cfg, deps); }
  catch (e) { return { ok: false, message: e instanceof UpstreamError ? e.message.slice(0, 400) : `${e.name}: ${String(e.message).slice(0, 300)}` }; }
}
export const publishTo = (platform, creds, cfg, payload, deps = defaultDeps()) => REGISTRY[platform].publish(creds, cfg, payload, deps);
