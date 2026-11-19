// ALL HTTP IS MOCKED. These tests do not prove live publishing works.
import test from "node:test";
import assert from "node:assert/strict";
import * as S from "../src/social.js";
import { UpstreamError } from "../src/http.js";
import { deps, parseBody } from "./mock.mjs";

const pub = (p, c, cfg, payload, d) => S.publishTo(p, c, cfg, payload, d);
const G = "https://graph.facebook.com/v21.0", T = "https://graph.threads.net/v1.0";

test("registry: all eight platforms with documented permissions and required fields", () => {
  const sp = S.spec();
  assert.deepEqual(Object.keys(sp).sort(), ["facebook", "instagram", "linkedin", "pinterest", "reddit", "threads", "x", "youtube"]);
  for (const v of Object.values(sp)) { assert.ok(v.permissions.length > 20); assert.ok(v.required.length); }
  assert.deepEqual(S.missingFields("facebook", { page_id: "1" }), ["page_access_token"]);
});
test("formatText: per-platform limits, whitespace, titles", () => {
  assert.equal(S.formatText("x", "a".repeat(500)).text.length, 280);
  assert.ok(S.formatText("x", "a".repeat(500)).text.endsWith("\u2026"));
  assert.equal(S.formatText("threads", "b".repeat(900)).text.length, 500);
  assert.equal(S.formatText("reddit", "t".repeat(400), "T".repeat(400)).title.length, 300);
  assert.ok(S.formatText("pinterest", "x", "y".repeat(200)).title.endsWith("\u2026"));
  assert.equal(S.formatText("linkedin", "  padded  ").text, "padded");
});

test("Reddit: refresh-token flow, self post, errors and RATELIMIT retry", async () => {
  const c = { client_id: "cid", client_secret: "csec", refresh_token: "rt" };
  let d = deps([{ match: "https://www.reddit.com/api/v1/access_token", respond: { access_token: "AT" } }, { match: "https://oauth.reddit.com/api/v1/me", respond: { name: "bot" } },
    { match: "https://oauth.reddit.com/api/submit", respond: { json: { errors: [], data: { id: "abc", url: "https://reddit.com/r/x/abc" } } } }]);
  assert.deepEqual(await S.testAccount("reddit", c, {}, d), { ok: true, account: "bot" });
  assert.deepEqual(await pub("reddit", c, {}, { subreddit: "test", title: "Hi", text: "body" }, d), { id: "abc", url: "https://reddit.com/r/x/abc" });
  const sub = d.calls.at(-1); assert.deepEqual([parseBody(sub.body).sr, parseBody(sub.body).kind], ["test", "self"]);
  assert.equal(sub.headers.authorization, "Bearer AT"); assert.ok(d.calls[0].headers.authorization.startsWith("Basic "));
  d = deps([{ match: "https://oauth.reddit.com/api/submit", respond: { json: { errors: [["RATELIMIT", "slow", "x"]] } } }]);
  await assert.rejects(pub("reddit", { client_id: "a", client_secret: "b", access_token: "AT" }, {}, { subreddit: "t", title: "x" }, d), (e) => e.retryable === true);
  d = deps([{ match: "https://oauth.reddit.com/api/submit", respond: { json: { errors: [["SUBREDDIT_NOEXIST", "no", "sr"]] } } }]);
  await assert.rejects(pub("reddit", { client_id: "a", client_secret: "b", access_token: "AT" }, {}, { subreddit: "t", title: "x" }, d), (e) => e.retryable === false);
  await assert.rejects(pub("reddit", { client_id: "a", client_secret: "b", access_token: "AT" }, {}, { title: "x" }, d), /subreddit/);
});

test("Facebook: text/photo/video/scheduled construction; error mapping hides the token", async () => {
  const c = { page_id: "P1", page_access_token: "PTOK-secret-123" };
  const d = deps([{ match: `${G}/P1?`, respond: { name: "My Page" } }, { match: `${G}/P1/feed`, respond: { id: "P1_1" } }, { match: `${G}/P1/photos`, respond: { post_id: "P1_2" } }, { match: `${G}/P1/videos`, respond: { id: "V1" } }]);
  assert.equal((await S.testAccount("facebook", c, {}, d)).account, "My Page");
  assert.deepEqual(await pub("facebook", c, {}, { text: "hello", link: "https://x.y" }, d), { id: "P1_1" });
  assert.deepEqual([parseBody(d.calls.at(-1).body).message, parseBody(d.calls.at(-1).body).link, parseBody(d.calls.at(-1).body).access_token], ["hello", "https://x.y", "PTOK-secret-123"]);
  assert.equal((await pub("facebook", c, {}, { text: "cap", media_url: "https://i/p.jpg" }, d)).id, "P1_2");
  await pub("facebook", c, {}, { text: "v", media_url: "https://i/v.mp4", media_type: "video", scheduled_publish_time: 1900000000 }, d);
  const b = parseBody(d.calls.at(-1).body); assert.deepEqual([b.file_url, b.published, b.scheduled_publish_time], ["https://i/v.mp4", "false", "1900000000"]);
  const bad = deps([{ match: `${G}/P1/feed`, respond: new Response("token PTOK-secret-123 expired", { status: 401 }) }]);
  await assert.rejects(pub("facebook", c, {}, { text: "x" }, bad), (e) => e instanceof UpstreamError && !e.retryable && !e.message.includes("PTOK-secret") && /permissions/.test(e.message));
  const rl = deps([{ match: `${G}/P1/feed`, respond: new Response("slow", { status: 429, headers: { "retry-after": "120" } }) }]);
  await assert.rejects(pub("facebook", c, {}, { text: "x" }, rl), (e) => e.retryable && e.retryAfter === 120);
});

test("Instagram: image two-step publish; requires media; Reel polls until FINISHED, fails on ERROR", async () => {
  const c = { ig_user_id: "IG1", access_token: "TOK" };
  let d = deps([{ match: `${G}/IG1?`, respond: { username: "brand" } }, { match: `${G}/IG1/media_publish`, respond: { id: "MEDIA9" } }, { match: `${G}/IG1/media`, respond: { id: "CONT1" } }]);
  assert.equal((await S.testAccount("instagram", c, {}, d)).account, "brand");
  assert.deepEqual(await pub("instagram", c, {}, { text: "cap", media_url: "https://i/p.jpg" }, d), { id: "MEDIA9" });
  assert.equal(parseBody(d.calls.at(-2).body).image_url, "https://i/p.jpg"); assert.equal(parseBody(d.calls.at(-1).body).creation_id, "CONT1");
  await assert.rejects(pub("instagram", c, {}, { text: "no media" }, d), /media_url/);
  let polls = 0;
  d = deps([{ match: `${G}/IG1/media_publish`, respond: { id: "R1" } }, { match: `${G}/IG1/media`, respond: { id: "C2" } }, { match: `${G}/C2?`, respond: () => Response.json({ status_code: ++polls < 3 ? "IN_PROGRESS" : "FINISHED" }) }]);
  const out = await pub("instagram", c, {}, { text: "t", media_url: "https://i/v.mp4", media_type: "video" }, d);
  assert.equal(out.id, "R1"); assert.equal(polls, 3); assert.equal(parseBody(d.calls[0].body).media_type, "REELS");
  d = deps([{ match: `${G}/IG1/media`, respond: { id: "C3" } }, { match: `${G}/C3?`, respond: { status_code: "ERROR" } }]);
  await assert.rejects(pub("instagram", c, {}, { text: "t", media_url: "https://i/v.mp4", media_type: "video" }, d), /processing failed/);
});

test("Threads: text flow and image flow with processing wait", async () => {
  const c = { user_id: "U1", access_token: "TT" };
  let d = deps([{ match: `${T}/me?`, respond: { username: "thr" } }, { match: `${T}/U1/threads_publish`, respond: { id: "TH9" } }, { match: `${T}/U1/threads`, respond: { id: "CT1" } }]);
  assert.equal((await S.testAccount("threads", c, {}, d)).account, "thr");
  assert.deepEqual(await pub("threads", c, {}, { text: "hello" }, d), { id: "TH9" });
  assert.equal(parseBody(d.calls.at(-2).body).media_type, "TEXT"); assert.equal(parseBody(d.calls.at(-1).body).creation_id, "CT1");
  d = deps([{ match: `${T}/U1/threads_publish`, respond: { id: "TH2" } }, { match: `${T}/U1/threads`, respond: { id: "CT2" } }, { match: `${T}/CT2?`, respond: { status: "FINISHED" } }]);
  await pub("threads", c, {}, { text: "x", media_url: "https://i/p.png" }, d);
  assert.equal(parseBody(d.calls[0].body).media_type, "IMAGE");
});

test("Pinterest: pin construction and validation", async () => {
  const c = { access_token: "PIN", board_id: "B1" };
  const d = deps([{ match: "https://api.pinterest.com/v5/user_account", respond: { username: "pinner" } }, { match: "https://api.pinterest.com/v5/pins", respond: { id: "PIN77" } }]);
  assert.equal((await S.testAccount("pinterest", c, {}, d)).account, "pinner");
  assert.deepEqual(await pub("pinterest", c, {}, { text: "desc", title: "T", media_url: "https://i/p.jpg", link: "https://s" }, d), { id: "PIN77" });
  const b = JSON.parse(d.calls.at(-1).body);
  assert.deepEqual([b.board_id, b.media_source, b.title], ["B1", { source_type: "image_url", url: "https://i/p.jpg" }, "T"]);
  await assert.rejects(pub("pinterest", c, {}, { text: "no image" }, d), /media_url/);
});

test("X: text and media tweet; 429 retryable with Retry-After; 403 permanent; refresh token flow", async () => {
  const c = { access_token: "XTOK" };
  let d = deps([{ match: "https://api.x.com/2/users/me", respond: { data: { username: "me_x" } } }, { match: "https://api.x.com/2/tweets", respond: { data: { id: "123" } } },
    { match: "https://i/p.jpg", respond: new Response(new Uint8Array([1, 2, 3]), { headers: { "content-type": "image/jpeg" } }) }, { match: "https://api.x.com/2/media/upload", respond: { data: { id: "M1" } } }]);
  assert.equal((await S.testAccount("x", c, {}, d)).account, "me_x");
  const out = await pub("x", c, {}, { text: "hi" }, d);
  assert.equal(out.id, "123"); assert.deepEqual(JSON.parse(d.calls.at(-1).body), { text: "hi" }); assert.equal(d.calls.at(-1).headers.authorization, "Bearer XTOK");
  await pub("x", c, {}, { text: "pic", media_url: "https://i/p.jpg" }, d);
  assert.deepEqual(JSON.parse(d.calls.at(-1).body).media, { media_ids: ["M1"] });
  assert.equal(d.calls.find((x) => x.url.endsWith("/media/upload")).body.media_category, "tweet_image");
  d = deps([{ match: "https://api.x.com/2/tweets", respond: new Response("Too Many Requests", { status: 429, headers: { "retry-after": "900" } }) }]);
  await assert.rejects(pub("x", c, {}, { text: "hi" }, d), (e) => e.retryable && e.retryAfter === 900);
  d = deps([{ match: "https://api.x.com/2/tweets", respond: new Response("app-only token cannot post", { status: 403 }) }]);
  await assert.rejects(pub("x", c, {}, { text: "hi" }, d), (e) => !e.retryable);
  d = deps([{ match: "https://api.x.com/2/oauth2/token", respond: { access_token: "NEW" } }, { match: "https://api.x.com/2/tweets", respond: { data: { id: "9" } } }]);
  await pub("x", { access_token: "old", refresh_token: "RT", client_id: "CID", client_secret: "CS" }, {}, { text: "hi" }, d);
  assert.equal(d.calls.at(-1).headers.authorization, "Bearer NEW");
});

test("LinkedIn: text post headers + id; image upload flow", async () => {
  const c = { access_token: "LTOK", author_urn: "urn:li:person:abc" };
  let d = deps([{ match: "https://api.linkedin.com/v2/userinfo", respond: { name: "Ada" } }, { match: "https://api.linkedin.com/rest/posts", respond: new Response(null, { status: 201, headers: { "x-restli-id": "urn:li:share:1" } }) }]);
  assert.equal((await S.testAccount("linkedin", c, {}, d)).account, "Ada");
  assert.deepEqual(await pub("linkedin", c, {}, { text: "post" }, d), { id: "urn:li:share:1" });
  const h = d.calls.at(-1).headers; assert.ok(h["linkedin-version"]); assert.equal(h["x-restli-protocol-version"], "2.0.0");
  const b = JSON.parse(d.calls.at(-1).body); assert.deepEqual([b.author, b.commentary, b.lifecycleState], ["urn:li:person:abc", "post", "PUBLISHED"]);
  d = deps([{ match: "https://i/p.jpg", respond: new Response(new Uint8Array([1])) }, { match: "https://api.linkedin.com/rest/images?action=initializeUpload", respond: { value: { uploadUrl: "https://upload.li/x", image: "urn:li:image:9" } } },
    { match: "https://upload.li/x", method: "PUT", respond: new Response(null, { status: 201 }) }, { match: "https://api.linkedin.com/rest/posts", respond: new Response(null, { status: 201, headers: { "x-restli-id": "urn:li:share:2" } }) }]);
  await pub("linkedin", c, {}, { text: "pic", media_url: "https://i/p.jpg" }, d);
  assert.equal(JSON.parse(d.calls.at(-1).body).content.media.id, "urn:li:image:9");
});

test("YouTube: resumable upload flow, private by default, size limit, requires media", async () => {
  const c = { client_id: "a", client_secret: "b", refresh_token: "rt" };
  let d = deps([{ match: "https://oauth2.googleapis.com/token", respond: { access_token: "YT" } }, { match: "https://www.googleapis.com/youtube/v3/channels", respond: { items: [{ snippet: { title: "Chan" } }] } },
    { match: "https://v/video.mp4", respond: new Response(new Uint8Array(10), { headers: { "content-type": "video/mp4" } }) },
    { match: "https://www.googleapis.com/upload/youtube/v3/videos", respond: new Response("{}", { headers: { location: "https://upload.google/session1" } }) }, { match: "https://upload.google/session1", method: "PUT", respond: { id: "vid123" } }]);
  assert.equal((await S.testAccount("youtube", c, {}, d)).account, "Chan");
  assert.deepEqual(await pub("youtube", c, {}, { title: "T", text: "desc", media_url: "https://v/video.mp4" }, d), { id: "vid123", url: "https://youtu.be/vid123" });
  const meta = JSON.parse(d.calls.find((x) => x.url.includes("uploadType=resumable")).body);
  assert.deepEqual([meta.snippet.title, meta.status.privacyStatus], ["T", "private"]);
  await assert.rejects(pub("youtube", c, {}, { title: "no media" }, d), /media_url/);
  d = deps([{ match: "https://oauth2.googleapis.com/token", respond: { access_token: "YT" } }, { match: "https://v/big.mp4", respond: new Response(new Uint8Array(61 * 1024 * 1024)) }]);
  await assert.rejects(pub("youtube", c, {}, { media_url: "https://v/big.mp4" }, d), /too large/);
});

test("connection tests never throw and never leak credentials", async () => {
  const d = deps([{ match: "https://api.pinterest.com/v5/user_account", respond: new Response("invalid BAD-token-12345", { status: 401 }) }]);
  const r = await S.testAccount("pinterest", { access_token: "BAD-token-12345", board_id: "1" }, {}, d);
  assert.equal(r.ok, false); assert.ok(!r.message.includes("BAD-token-12345")); assert.match(r.message, /401/);
  const net = { fetchFn: async () => { throw new Error("connect ECONNREFUSED BAD-token-12345"); }, sleep: async () => {} };
  const r2 = await S.testAccount("pinterest", { access_token: "BAD-token-12345", board_id: "1" }, {}, net);
  assert.equal(r2.ok, false); assert.ok(!r2.message.includes("BAD-token-12345"));
});
