// Real Worker runtime (workerd) + real D1; only the OUTBOUND internet is mocked.
import test from "node:test";
import assert from "node:assert/strict";
import { makeWorker, SECRETS, waitFor } from "./harness.mjs";

const llmOK = (text = "OK") => Response.json({ choices: [{ message: { content: text } }] });
const N8N = { "x-addon-token": SECRETS.N8N_ADDON_API_TOKEN };

test("LLM settings: save, masked, encrypted at rest, edit keeps key, default, per-provider test, delete", async () => {
  const h = await makeWorker({ outbound: async () => llmOK() }); await h.login();
  try {
    const a = (await h.req("/api/settings/llm", { method: "POST", body: { name: "DS", provider: "deepseek", model: "deepseek-chat", api_key: "sk-abcdef1234567890WXYZ" } })).data;
    assert.ok(a.is_default); assert.ok(a.api_key_masked.endsWith("WXYZ"));
    const blob = JSON.stringify((await h.req("/api/settings/llm")).data) + JSON.stringify(a);
    assert.ok(!blob.includes("sk-abcdef1234567890"));
    const raw = JSON.stringify(await h.q("SELECT config_json, secret_enc FROM settings WHERE kind='llm'"));
    assert.ok(!raw.includes("abcdef1234567890"), "key must be encrypted at rest");
    const b = (await h.req("/api/settings/llm", { method: "POST", body: { name: "OAI", provider: "openai", model: "gpt-4o-mini", api_key: "sk-other-000000000000" } })).data;
    assert.equal(b.is_default, false);
    await h.req(`/api/settings/llm/${b.id}/default`, { method: "POST" });
    const flags = Object.fromEntries((await h.req("/api/settings/llm")).data.map((x) => [x.id, x.is_default])); assert.ok(flags[b.id] && !flags[a.id]);
    const u = (await h.req(`/api/settings/llm/${a.id}`, { method: "PUT", body: { model: "deepseek-reasoner" } })).data;
    assert.equal(u.model, "deepseek-reasoner"); assert.ok(u.api_key_masked.endsWith("WXYZ"));
    assert.equal((await h.req(`/api/settings/llm/${a.id}/test`, { method: "POST" })).data.ok, true);
    const sent = h.calls.at(-1); assert.equal(sent.headers.authorization, "Bearer sk-abcdef1234567890WXYZ"); assert.equal(JSON.parse(sent.body).model, "deepseek-reasoner");
    assert.equal((await h.req(`/api/settings/llm/${a.id}`, { method: "DELETE" })).status, 200);
    assert.equal((await h.req("/api/settings/llm")).data.length, 1);
    assert.equal((await h.req("/api/settings/llm", { method: "POST", body: { name: "x", provider: "nope", model: "m" } })).status, 422);
    assert.equal((await h.req("/api/settings/llm", { method: "POST", body: { name: "x", provider: "custom", model: "m", api_key: "k" } })).status, 400);
  } finally { await h.dispose(); }
});

test("n8n generate uses the default provider and honours a per-workflow model override", async () => {
  const h = await makeWorker({ outbound: async () => llmOK("generated post") }); await h.login();
  try {
    const a = (await h.req("/api/settings/llm", { method: "POST", body: { name: "DS", provider: "deepseek", model: "deepseek-chat", api_key: "KEY-123456789" } })).data;
    const r = await h.req("/api/n8n/generate", { method: "POST", body: { prompt: "hello", model: "deepseek-reasoner" }, headers: N8N });
    assert.equal(r.status, 200); assert.deepEqual([r.data.text, r.data.model], ["generated post", "deepseek-reasoner"]);
    assert.equal(JSON.parse(h.calls.at(-1).body).model, "deepseek-reasoner");
    assert.equal((await h.req("/api/n8n/generate", { method: "POST", body: { prompt: "x" } })).status, 401);
    assert.equal((await h.req("/api/n8n/generate", { method: "POST", body: {}, headers: N8N })).status, 400);
    assert.equal((await h.req("/api/n8n/generate", { method: "POST", body: { prompt: "x", provider_id: "missing" }, headers: N8N })).status, 404);
    const acc = (await h.req("/api/n8n/accounts", { headers: N8N })).data; assert.equal(acc.llm[0].id, a.id);
  } finally { await h.dispose(); }
});

test("social settings: masked, encrypted, edit keeps old secrets, validation, delete", async () => {
  const h = await makeWorker(); await h.login();
  try {
    const a = (await h.req("/api/settings/social", { method: "POST", body: { platform: "facebook", name: "Main", secrets: { page_id: "123456789012", page_access_token: "EAAB-very-secret-token-0042" } } })).data;
    const blob = JSON.stringify((await h.req("/api/settings/social")).data) + JSON.stringify(a);
    assert.ok(!blob.includes("EAAB-very-secret")); assert.ok(a.secrets_masked.page_access_token.endsWith("0042"));
    assert.ok(!JSON.stringify(await h.q("SELECT * FROM settings")).includes("EAAB-very-secret"), "encrypted at rest");
    assert.equal((await h.req("/api/settings/social", { method: "POST", body: { platform: "facebook", secrets: { page_id: "1" } } })).status, 400);
    assert.equal((await h.req("/api/settings/social", { method: "POST", body: { platform: "myspace", secrets: {} } })).status, 400);
    const u = (await h.req(`/api/settings/social/${a.id}`, { method: "PUT", body: { name: "Renamed", secrets: { page_id: "999" } } })).data;
    assert.equal(u.label, "Renamed"); assert.ok(u.secrets_masked.page_access_token.endsWith("0042"));
    assert.equal((await h.req("/api/settings/social/platforms")).data.x.text_limit, 280);
    assert.equal((await h.req(`/api/settings/social/${a.id}`, { method: "DELETE" })).status, 200);
  } finally { await h.dispose(); }
});

test("social connection test failure is reported without leaking the token", async () => {
  const h = await makeWorker({ outbound: async () => new Response("invalid BAD-token-12345", { status: 401 }) }); await h.login();
  try {
    const a = (await h.req("/api/settings/social", { method: "POST", body: { platform: "pinterest", secrets: { access_token: "BAD-token-12345", board_id: "1" } } })).data;
    const r = (await h.req(`/api/settings/social/${a.id}/test`, { method: "POST" })).data;
    assert.equal(r.ok, false); assert.ok(!r.message.includes("BAD-token-12345"));
  } finally { await h.dispose(); }
});

test("end to end: queued job publishes via the Worker with platform formatting; token only in the request header; job result is clean", async () => {
  const h = await makeWorker({ outbound: async (req) => req.url.endsWith("/2/tweets") ? Response.json({ data: { id: "42" } }) : new Response("{}") }); await h.login();
  try {
    const a = (await h.req("/api/settings/social", { method: "POST", body: { platform: "x", secrets: { access_token: "XTOK-123456789" } } })).data;
    const id = (await h.req("/api/jobs", { method: "POST", body: { task: "social_publish", payload: { account_id: a.id, text: "z".repeat(400) } } })).data.id;
    assert.ok(await waitFor(async () => (await h.q("SELECT status FROM jobs WHERE id=?", id))[0].status === "completed", 15000));
    const j = (await h.req(`/api/jobs/${id}`)).data;
    assert.equal(j.result.id, "42"); assert.equal(JSON.parse(h.calls.find((c) => c.url.endsWith("/2/tweets")).body).text.length, 280);
    assert.ok(!JSON.stringify(j).includes("XTOK-123456789"));
    assert.deepEqual(j.logs.map((l) => l.message), ["Publishing to x", "Published to x: 42"]);
  } finally { await h.dispose(); }
});

test("end to end: ai_publish generates with the LLM, formats and publishes", async () => {
  const h = await makeWorker({ outbound: async (req) => req.url.includes("deepseek") ? llmOK("AI generated post text") : Response.json({ data: { id: "7" } }) }); await h.login();
  try {
    await h.req("/api/settings/llm", { method: "POST", body: { name: "DS", provider: "deepseek", model: "deepseek-chat", api_key: "KEY-123456789" } });
    const a = (await h.req("/api/settings/social", { method: "POST", body: { platform: "x", secrets: { access_token: "XTOK-123456789" } } })).data;
    const id = (await h.req("/api/jobs", { method: "POST", body: { task: "ai_publish", payload: { account_id: a.id, prompt: "write about cats" } } })).data.id;
    assert.ok(await waitFor(async () => (await h.q("SELECT status FROM jobs WHERE id=?", id))[0].status === "completed", 15000));
    const j = (await h.req(`/api/jobs/${id}`)).data;
    assert.equal(j.result.generated_text, "AI generated post text");
    assert.equal(JSON.parse(h.calls.find((c) => c.url.endsWith("/2/tweets")).body).text, "AI generated post text");
  } finally { await h.dispose(); }
});

test("end to end: rate-limited publish is re-queued with the platform's Retry-After, never lost", async () => {
  const h = await makeWorker({ outbound: async () => new Response("limit", { status: 429, headers: { "retry-after": "600" } }) }); await h.login();
  try {
    const a = (await h.req("/api/settings/social", { method: "POST", body: { platform: "x", secrets: { access_token: "XTOK-123456789" } } })).data;
    const id = (await h.req("/api/jobs", { method: "POST", body: { task: "social_publish", payload: { account_id: a.id, text: "hi" } } })).data.id;
    assert.ok(await waitFor(async () => { const r = (await h.q("SELECT status, attempts FROM jobs WHERE id=?", id))[0]; return r.status === "queued" && r.attempts === 1; }, 15000));
    const r = (await h.q("SELECT run_after, error FROM jobs WHERE id=?", id))[0];
    assert.ok(r.run_after > Date.now() + 590000); assert.match(r.error, /429/);
  } finally { await h.dispose(); }
});

test("permanent publish errors (bad credentials) fail immediately with a clear message", async () => {
  const h = await makeWorker({ outbound: async () => new Response("unauthorized", { status: 401 }) }); await h.login();
  try {
    const a = (await h.req("/api/settings/social", { method: "POST", body: { platform: "x", secrets: { access_token: "XTOK-123456789" } } })).data;
    const id = (await h.req("/api/jobs", { method: "POST", body: { task: "social_publish", payload: { account_id: a.id, text: "hi" } } })).data.id;
    assert.ok(await waitFor(async () => (await h.q("SELECT status FROM jobs WHERE id=?", id))[0].status === "failed", 15000));
    assert.match((await h.q("SELECT error FROM jobs WHERE id=?", id))[0].error, /401.*permissions/);
  } finally { await h.dispose(); }
});

test("job creation validates account and task", async () => {
  const h = await makeWorker(); await h.login();
  try {
    assert.equal((await h.req("/api/jobs", { method: "POST", body: { task: "social_publish", payload: { account_id: "nope" } } })).status, 400);
    assert.equal((await h.req("/api/jobs", { method: "POST", body: { task: "llm_prompt", payload: {} } })).status, 400);
    assert.equal((await h.req("/api/jobs", { method: "POST", body: { task: "evil" } })).status, 400);
  } finally { await h.dispose(); }
});

test("GitHub settings: token masked and encrypted; test endpoint works with the stored token", async () => {
  const h = await makeWorker({ outbound: async () => Response.json({ name: "Suite remote job", state: "active" }) }); await h.login();
  try {
    const r = (await h.req("/api/settings/github", { method: "PUT", body: { owner: "me", repo: "runner", workflow: "remote-job.yml", n8n_workflow: "n8n-host.yml", ref: "main", token: "github_pat_TOPSECRETVALUE0123456789" } })).data;
    assert.ok(!JSON.stringify(r).includes("TOPSECRETVALUE")); assert.ok(r.token_masked.endsWith("6789"));
    assert.ok(!JSON.stringify(await h.q("SELECT * FROM settings")).includes("TOPSECRETVALUE"));
    assert.ok(!JSON.stringify((await h.req("/api/settings/github")).data).includes("TOPSECRETVALUE"));
    assert.deepEqual((await h.req("/api/settings/github/test", { method: "POST" })).data, { ok: true, workflow: "Suite remote job", state: "active" });
    assert.equal(h.calls.at(-1).headers.authorization, "Bearer github_pat_TOPSECRETVALUE0123456789");
    await h.req("/api/settings/github", { method: "PUT", body: { owner: "me2", repo: "runner", workflow: "remote-job.yml", ref: "main" } });   // blank token keeps the old one
    await h.req("/api/settings/github/test", { method: "POST" });
    assert.equal(h.calls.at(-1).headers.authorization, "Bearer github_pat_TOPSECRETVALUE0123456789");
  } finally { await h.dispose(); }
});
