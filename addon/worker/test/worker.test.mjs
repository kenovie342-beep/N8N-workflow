// Runs under plain Node 20+ (WebCrypto + fetch built in) with in-memory D1/R2 fakes.
import test from "node:test";
import assert from "node:assert/strict";
import worker, { signMessage, retryUndelivered } from "../src/index.js";

const SECRET = "cb-secret-for-tests";

class FakeD1 {
  constructor() { this.rows = new Map(); }
  prepare(sql) {
    const self = this;
    return { sql, args: [], bind(...a) { this.args = a; return this; },
      async run() {
        if (sql.startsWith("INSERT OR IGNORE INTO callbacks")) {
          const [nonce, job_id, status, body, created_at] = this.args; const delivered = 0;
          if (self.rows.has(nonce)) return { meta: { changes: 0 } };
          self.rows.set(nonce, { nonce, job_id, status, body, delivered, attempts: 0, created_at });
          return { meta: { changes: 1 } };
        }
        if (sql.startsWith("UPDATE callbacks SET delivered = 1")) { self.rows.get(this.args[0]).delivered = 1; return { meta: { changes: 1 } }; }
        if (sql.startsWith("UPDATE callbacks SET attempts")) { self.rows.get(this.args[0]).attempts++; return { meta: { changes: 1 } }; }
        if (sql.startsWith("DELETE FROM callbacks")) return { meta: { changes: 0 } };
        throw new Error("unexpected sql " + sql);
      },
      async first() { return { 1: 1 }; },
      async all() { return { results: [...self.rows.values()].filter((r) => !r.delivered && r.attempts < 20) }; } };
  }
}
class FakeR2 { constructor() { this.m = new Map(); }
  async put(k, body, opts) { const buf = new Uint8Array(await new Response(body).arrayBuffer()); this.m.set(k, { buf, opts }); }
  async get(k) { const o = this.m.get(k); return o ? { body: o.buf, httpMetadata: o.opts.httpMetadata } : null; } }

const mkEnv = () => ({ DB: new FakeD1(), BUCKET: new FakeR2(), CALLBACK_SECRET: SECRET, ORIGIN_URL: "https://origin.test" });

async function signedReq(path, bodyObj, { secret = SECRET, ts, nonce } = {}) {
  const body = typeof bodyObj === "string" ? bodyObj : JSON.stringify(bodyObj);
  ts = ts ?? String(Math.floor(Date.now() / 1000));
  nonce = nonce ?? crypto.randomUUID().replaceAll("-", "");
  const sig = await signMessage(secret, ts, nonce, body);
  return new Request("https://w.test" + path, { method: "POST", body,
    headers: { "x-suite-timestamp": ts, "x-suite-nonce": nonce, "x-suite-signature": sig } });
}

function mockOrigin(status = 200) {
  const calls = [];
  globalThis.fetch = async (url, init) => { calls.push({ url, init }); return new Response("{}", { status }); };
  return calls;
}

test("health works", async () => {
  const r = await worker.fetch(new Request("https://w.test/health"), mkEnv());
  assert.equal(r.status, 200);
});

test("callback: valid signature is stored and forwarded with a fresh signature", async () => {
  const env = mkEnv(); const calls = mockOrigin();
  const r = await worker.fetch(await signedReq("/callback", { job_id: "j1", status: "completed" }), env);
  assert.equal(r.status, 200); assert.equal((await r.json()).delivered, true);
  assert.equal(calls.length, 1); assert.equal(calls[0].url, "https://origin.test/api/runner/callback");
  const h = calls[0].init.headers;
  assert.equal(h["x-suite-signature"], await signMessage(SECRET, h["x-suite-timestamp"], h["x-suite-nonce"], calls[0].init.body));
});

test("callback: bad signature / stale / tampered / missing are rejected and not forwarded", async () => {
  const env = mkEnv(); const calls = mockOrigin();
  assert.equal((await worker.fetch(await signedReq("/callback", { job_id: "j", status: "completed" }, { secret: "nope" }), env)).status, 401);
  assert.equal((await worker.fetch(await signedReq("/callback", { job_id: "j", status: "completed" }, { ts: "1000" }), env)).status, 401);
  const req = await signedReq("/callback", { job_id: "j", status: "completed" });
  const tampered = new Request(req.url, { method: "POST", headers: req.headers, body: JSON.stringify({ job_id: "j", status: "failed" }) });
  assert.equal((await worker.fetch(tampered, env)).status, 401);
  assert.equal((await worker.fetch(new Request("https://w.test/callback", { method: "POST", body: "{}" }), env)).status, 401);
  assert.equal(calls.length, 0);
});

test("callback: replayed nonce is a no-op duplicate", async () => {
  const env = mkEnv(); const calls = mockOrigin();
  const body = { job_id: "j9", status: "completed" };
  assert.equal((await worker.fetch(await signedReq("/callback", body, { nonce: "n-fixed" }), env)).status, 200);
  const again = await worker.fetch(await signedReq("/callback", body, { nonce: "n-fixed" }), env);
  assert.equal((await again.json()).duplicate, true);
  assert.equal(calls.length, 1);
});

test("callback: origin outage -> stored, 202, then delivered by scheduled retry", async () => {
  const env = mkEnv(); mockOrigin(503);
  const r = await worker.fetch(await signedReq("/callback", { job_id: "j2", status: "failed", error: "x" }), env);
  assert.equal(r.status, 202);
  assert.equal([...env.DB.rows.values()][0].delivered, 0);
  const calls = mockOrigin(200);
  await worker.scheduled({}, env);
  assert.equal([...env.DB.rows.values()][0].delivered, 1);
  assert.ok(calls.some((c) => c.url.endsWith("/api/cf/tick")));
  assert.ok(calls.some((c) => c.url.endsWith("/api/runner/callback")));
});

test("callback: invalid body shape rejected", async () => {
  const env = mkEnv(); mockOrigin();
  assert.equal((await worker.fetch(await signedReq("/callback", { job_id: "j", status: "weird" }), env)).status, 400);
});

test("signed R2 URLs: mint, upload, download, expiry, tamper, traversal", async () => {
  const env = mkEnv();
  const mint = async (key, method) => (await (await worker.fetch(await signedReq("/sign-url", { key, method }), env)).json());
  const up = await mint("media/pic.png", "PUT");
  assert.ok(up.url.includes("/r2/media/pic.png?exp="));
  const put = await worker.fetch(new Request(up.url, { method: "PUT", body: "PNGDATA", headers: { "content-type": "image/png" } }), env);
  assert.equal(put.status, 200);
  const dl = await mint("media/pic.png", "GET");
  const got = await worker.fetch(new Request(dl.url), env);
  assert.equal(await got.text(), "PNGDATA"); assert.equal(got.headers.get("content-type"), "image/png");
  // a GET link cannot be used to PUT, and tampering fails
  assert.equal((await worker.fetch(new Request(dl.url, { method: "PUT", body: "x" }), env)).status, 403);
  assert.equal((await worker.fetch(new Request(dl.url.replace("pic.png", "other.png")), env)).status, 403);
  // expired link
  const key = "media/pic.png", exp = Math.floor(Date.now() / 1000) - 5;
  const sig = Buffer.from(await signMessage(SECRET, "x", "y", "z")).toString(); // wrong anyway
  assert.equal((await worker.fetch(new Request(`https://w.test/r2/${key}?exp=${exp}&sig=${sig}`), env)).status, 403);
  // minting requires a valid signature and rejects traversal
  assert.equal((await worker.fetch(await signedReq("/sign-url", { key: "a" }, { secret: "bad" }), env)).status, 401);
  assert.equal((await worker.fetch(await signedReq("/sign-url", { key: "../etc/passwd" }), env)).status, 400);
});

test("internal errors never leak details", async () => {
  const env = mkEnv(); env.DB = { prepare() { throw new Error("secret connection string"); } };
  const r = await worker.fetch(new Request("https://w.test/health"), env);
  assert.equal(r.status, 500); assert.ok(!(await r.text()).includes("secret"));
});
