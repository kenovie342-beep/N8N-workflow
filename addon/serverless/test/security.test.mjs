import test from "node:test";
import assert from "node:assert/strict";
import { makeWorker, SECRETS, KEY } from "./harness.mjs";
import { decryptJSON, encryptJSON, makeSession, mask, readSession, scrub, signMessage } from "../src/crypto.js";

test("AES-GCM round trip; ciphertext hides the secret; wrong key fails; tampering fails", async () => {
  const blob = await encryptJSON(KEY, { api_key: "sk-secret-value-123456" });
  assert.ok(!blob.includes("secret-value"));
  assert.deepEqual(await decryptJSON(KEY, blob), { api_key: "sk-secret-value-123456" });
  await assert.rejects(decryptJSON(Buffer.alloc(32, 9).toString("base64"), blob), /Cannot decrypt/);
  await assert.rejects(decryptJSON(KEY, blob.slice(0, -4) + "AAAA"), /Cannot decrypt/);
  assert.notEqual(blob, await encryptJSON(KEY, { api_key: "sk-secret-value-123456" }), "random IV per encryption");
});
test("masking never reveals short secrets", () => {
  assert.equal(mask("short"), "\u2022".repeat(8));
  assert.ok(mask("a-long-secret-value-9876").endsWith("9876"));
});
test("scrub removes known and patterned secrets", () => {
  const out = scrub("github_pat_11ABCDEFGH0123456789abcdefghijkl Authorization: Bearer abcdef1234567890 ?api_key=SUPERSECRET1 sk-abcdefghijklmnop1234 mine-private-value", ["mine-private-value"]);
  for (const leak of ["github_pat_11ABCDEF", "abcdef1234567890", "SUPERSECRET1", "sk-abcdefghijklmnop", "mine-private-value"]) assert.ok(!out.includes(leak), leak);
});
test("sessions: valid, tampered, expired", async () => {
  const { token } = await makeSession("s".repeat(48), "admin");
  assert.equal((await readSession("s".repeat(48), token)).u, "admin");
  assert.equal(await readSession("s".repeat(48), token.slice(0, -3) + "xxx"), null);
  assert.equal(await readSession("other".repeat(10), token), null);
  const real = Date.now; Date.now = () => real() + 13 * 3600e3;
  try { assert.equal(await readSession("s".repeat(48), token), null); } finally { Date.now = real; }
});

test("API requires login; wrong password rejected; CSRF enforced; login is rate limited", async () => {
  const h = await makeWorker();
  try {
    assert.equal((await h.req("/api/jobs")).status, 401);
    assert.equal((await h.req("/api/stats")).status, 401);
    assert.equal((await h.req("/api/auth/login", { method: "POST", body: { username: "admin", password: "wrong" } })).status, 401);
    assert.equal((await h.req("/api/auth/login", { method: "POST", body: { username: "root", password: SECRETS.ADMIN_PASSWORD } })).status, 401);
    await h.login();
    const saved = h.csrf; h.csrf = null;
    assert.equal((await h.req("/api/jobs", { method: "POST", body: { task: "noop" } })).status, 403);
    h.csrf = saved;
    assert.equal((await h.req("/api/jobs", { method: "POST", body: { task: "noop" } })).status, 200);
    const tampered = h.cookie.slice(0, -3) + "zzz"; h.cookie = tampered;
    assert.equal((await h.req("/api/jobs")).status, 401);
    const codes = []; const g = await makeWorker();
    for (let i = 0; i < 12; i++) codes.push((await g.req("/api/auth/login", { method: "POST", body: { username: "a", password: "b" } })).status);
    assert.equal(codes[0], 401); assert.ok(codes.includes(429)); await g.dispose();
  } finally { await h.dispose(); }
});

test("login impossible when ADMIN_PASSWORD is not configured", async () => {
  const h = await makeWorker({ vars: { ADMIN_PASSWORD: "" } });
  try { assert.equal((await h.req("/api/auth/login", { method: "POST", body: { username: "admin", password: "" } })).status, 401); } finally { await h.dispose(); }
});

test("n8n endpoints need the add-on token; security headers present; errors never leak internals", async () => {
  const h = await makeWorker();
  try {
    assert.equal((await h.req("/api/n8n/jobs", { method: "POST", body: { task: "noop" } })).status, 401);
    assert.equal((await h.req("/api/n8n/jobs", { method: "POST", body: { task: "noop" }, headers: { "x-addon-token": "wrong" } })).status, 401);
    const ok = await h.req("/api/n8n/jobs", { method: "POST", body: { task: "noop" }, headers: { "x-addon-token": SECRETS.N8N_ADDON_API_TOKEN } });
    assert.equal(ok.status, 200);
    const st = await h.req(`/api/n8n/jobs/${ok.data.id}`, { headers: { "x-addon-token": SECRETS.N8N_ADDON_API_TOKEN } });
    assert.ok(["queued", "running", "completed"].includes(st.data.status));
    const r = await h.req("/health"); assert.equal(r.headers.get("x-frame-options"), "DENY"); assert.equal(r.headers.get("x-content-type-options"), "nosniff");
    await h.login();
    const bad = await h.req("/api/jobs", { method: "POST", raw: "{not json", headers: { "content-type": "application/json" } });
    assert.equal(bad.status, 400);
    assert.equal((await h.req("/api/jobs", { method: "POST", body: { task: "evil" } })).status, 400);
    assert.equal((await h.req("/api/nope")).status, 404);
  } finally { await h.dispose(); }
});

test("job payload secrets are masked by the API", async () => {
  const h = await makeWorker(); await h.login();
  try {
    const id = (await h.req("/api/jobs", { method: "POST", body: { task: "noop", payload: { api_key: "SHOULD-NOT-SHOW", ok: 1 }, run_after: new Date(Date.now() + 1e7).toISOString() } })).data.id;
    const d = (await h.req(`/api/jobs/${id}`)).data;
    assert.equal(d.payload.api_key, "***"); assert.equal(d.payload.ok, 1);
  } finally { await h.dispose(); }
});
