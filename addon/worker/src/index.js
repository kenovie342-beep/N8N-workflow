// Cloudflare Worker: lightweight control plane for the n8n Automation Suite.
//  - POST /callback        GitHub runner -> verify HMAC, replay-protect (D1), store, forward to origin
//  - GET/PUT /r2/<key>     signed upload/download URLs backed by R2
//  - POST /sign-url        (HMAC-authenticated) mint a signed R2 URL
//  - scheduled()           cron: ping origin /api/cf/tick, retry undelivered callbacks
// Heavy work (AI rendering, long jobs) is NOT done here.

const enc = new TextEncoder();
const SKEW = 300;

async function hmacHex(secret, message) {
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function safeEqual(a, b) {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

export async function signMessage(secret, ts, nonce, body) {
  return hmacHex(secret, `${ts}.${nonce}.${body}`);
}

export async function verifySigned(request, body, secret, now = Date.now() / 1000) {
  const ts = request.headers.get("x-suite-timestamp") || "";
  const nonce = request.headers.get("x-suite-nonce") || "";
  const sig = request.headers.get("x-suite-signature") || "";
  if (!secret || !ts || !nonce || !sig) return { ok: false, status: 401, error: "missing signature" };
  if (!/^\d+$/.test(ts) || Math.abs(now - Number(ts)) > SKEW) return { ok: false, status: 401, error: "stale" };
  if (!safeEqual(sig, await signMessage(secret, ts, nonce, body))) return { ok: false, status: 401, error: "bad signature" };
  return { ok: true, ts, nonce };
}

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json", "cache-control": "no-store" } });

async function forwardToOrigin(env, path, body, method = "POST") {
  const ts = String(Math.floor(Date.now() / 1000));
  const nonce = crypto.randomUUID().replaceAll("-", "");
  const sig = await signMessage(env.CALLBACK_SECRET, ts, nonce, body);
  const res = await fetch(env.ORIGIN_URL.replace(/\/$/, "") + path, {
    method,
    headers: { "content-type": "application/json", "x-suite-timestamp": ts, "x-suite-nonce": nonce, "x-suite-signature": sig },
    body: method === "GET" ? undefined : body,
  });
  return res;
}

async function handleCallback(request, env) {
  const body = await request.text();
  const v = await verifySigned(request, body, env.CALLBACK_SECRET);
  if (!v.ok) return json({ error: v.error }, v.status);
  let data;
  try { data = JSON.parse(body); } catch { return json({ error: "invalid json" }, 400); }
  if (!data.job_id || !["completed", "failed"].includes(data.status)) return json({ error: "invalid callback" }, 400);

  // Replay protection: first writer of (nonce) wins.
  const ins = await env.DB.prepare("INSERT OR IGNORE INTO callbacks (nonce, job_id, status, body, delivered, created_at) VALUES (?, ?, ?, ?, 0, ?)")
    .bind(v.nonce, data.job_id, data.status, body, Math.floor(Date.now() / 1000)).run();
  if (!ins.meta || ins.meta.changes === 0) return json({ ok: true, duplicate: true });

  try {
    const res = await forwardToOrigin(env, "/api/runner/callback", body);
    if (res.ok) {
      await env.DB.prepare("UPDATE callbacks SET delivered = 1 WHERE nonce = ?").bind(v.nonce).run();
      return json({ ok: true, delivered: true });
    }
  } catch (_) { /* fall through: stored for retry */ }
  return json({ ok: true, delivered: false, queued_for_retry: true }, 202);
}

export async function retryUndelivered(env, limit = 20) {
  const { results } = await env.DB.prepare("SELECT nonce, body, attempts FROM callbacks WHERE delivered = 0 AND attempts < 20 ORDER BY created_at LIMIT ?").bind(limit).all();
  let delivered = 0;
  for (const row of results || []) {
    try {
      const res = await forwardToOrigin(env, "/api/runner/callback", row.body);
      if (res.ok) { await env.DB.prepare("UPDATE callbacks SET delivered = 1 WHERE nonce = ?").bind(row.nonce).run(); delivered++; continue; }
    } catch (_) {}
    await env.DB.prepare("UPDATE callbacks SET attempts = attempts + 1 WHERE nonce = ?").bind(row.nonce).run();
  }
  return delivered;
}

// ---- signed R2 URLs ---------------------------------------------------------
const KEY_RE = /^[A-Za-z0-9._\-\/]{1,200}$/;
async function r2Sig(env, method, key, exp) { return hmacHex(env.URL_SIGNING_SECRET || env.CALLBACK_SECRET, `${method}:${key}:${exp}`); }

async function handleSignUrl(request, env, origin) {
  const body = await request.text();
  const v = await verifySigned(request, body, env.CALLBACK_SECRET);
  if (!v.ok) return json({ error: v.error }, v.status);
  const { key, method = "GET", ttl = 600 } = JSON.parse(body || "{}");
  if (!key || !KEY_RE.test(key) || key.includes("..") || !["GET", "PUT"].includes(method)) return json({ error: "invalid key/method" }, 400);
  const exp = Math.floor(Date.now() / 1000) + Math.min(Number(ttl) || 600, 3600);
  const sig = await r2Sig(env, method, key, exp);
  return json({ url: `${origin}/r2/${key}?exp=${exp}&sig=${sig}`, method, expires: exp });
}

async function handleR2(request, env, url) {
  const key = decodeURIComponent(url.pathname.slice("/r2/".length));
  const exp = Number(url.searchParams.get("exp") || 0);
  const sig = url.searchParams.get("sig") || "";
  const method = request.method;
  if (!["GET", "PUT"].includes(method)) return json({ error: "method not allowed" }, 405);
  if (!KEY_RE.test(key) || key.includes("..")) return json({ error: "invalid key" }, 400);
  if (!exp || exp < Date.now() / 1000) return json({ error: "link expired" }, 403);
  if (!safeEqual(sig, await r2Sig(env, method, key, exp))) return json({ error: "bad signature" }, 403);
  if (method === "PUT") {
    const len = Number(request.headers.get("content-length") || 0);
    if (len > 100 * 1024 * 1024) return json({ error: "too large" }, 413);
    await env.BUCKET.put(key, request.body, { httpMetadata: { contentType: request.headers.get("content-type") || "application/octet-stream" } });
    return json({ ok: true, key });
  }
  const obj = await env.BUCKET.get(key);
  if (!obj) return json({ error: "not found" }, 404);
  return new Response(obj.body, { headers: { "content-type": obj.httpMetadata?.contentType || "application/octet-stream", "cache-control": "private, max-age=60" } });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (url.pathname === "/health") {
        await env.DB.prepare("SELECT 1").first();
        return json({ ok: true, service: "suite-control-plane" });
      }
      if (url.pathname === "/callback" && request.method === "POST") return await handleCallback(request, env);
      if (url.pathname === "/sign-url" && request.method === "POST") return await handleSignUrl(request, env, url.origin);
      if (url.pathname.startsWith("/r2/")) return await handleR2(request, env, url);
      return json({ error: "not found" }, 404);
    } catch (e) {
      return json({ error: "internal error" }, 500); // never echo internals or secrets
    }
  },

  async scheduled(_event, env) {
    // Lightweight control-plane tick: nudge the origin scheduler, then retry any undelivered callbacks.
    try { await forwardToOrigin(env, "/api/cf/tick", "{}"); } catch (_) {}
    await retryUndelivered(env);
    await env.DB.prepare("DELETE FROM callbacks WHERE delivered = 1 AND created_at < ?").bind(Math.floor(Date.now() / 1000) - 7 * 86400).run();
  },
};
