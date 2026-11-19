import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { makeWorker } from "./harness.mjs";

const html = readFileSync(new URL("../web.html", import.meta.url), "utf8");
const js = /<script>([\s\S]*)<\/script>/.exec(html)[1];

test("dashboard is served by the Worker with strict headers, no demo data, no embedded secrets", async () => {
  const h = await makeWorker();
  try {
    const r = await h.mf.dispatchFetch("http://suite.test/");
    const text = await r.text();
    assert.equal(r.status, 200); assert.match(text, /Automation Suite/); assert.match(text, /viewport/);
    assert.match(r.headers.get("content-security-policy"), /frame-ancestors 'none'/);
    assert.ok(!/sk-[A-Za-z0-9]{16,}|github_pat_/.test(text)); assert.ok(!/demo|fake/i.test(text));
    assert.equal(text, html, "embedded UI must match web.html (run: node build-ui.mjs)");
  } finally { await h.dispose(); }
});

test("UI JavaScript parses", () => {
  const r = spawnSync("node", ["--check", "-"], { input: js, encoding: "utf8" });
  assert.equal(r.status, 0, r.stderr);
});

test("every GET endpoint the UI calls exists on the Worker and answers 200", async () => {
  const gets = [...js.matchAll(/api\("(\/[^"]+)"\)/g)].map((m) => m[1]).filter((p) => !p.includes("${"));
  const h = await makeWorker(); await h.login();
  try {
    assert.ok(gets.length >= 8, `found only ${gets.length}`);
    for (const p of new Set(gets)) { const r = await h.req("/api" + p); assert.equal(r.status, 200, `${p} -> ${r.status} ${JSON.stringify(r.data)}`); }
    for (const p of ["/api/jobs?limit=200", "/api/schedules/upcoming", "/api/n8n/status"]) assert.equal((await h.req(p)).status, 200);
  } finally { await h.dispose(); }
});

test("every mutating endpoint the UI calls is routed (never 404 for a valid id)", async () => {
  const h = await makeWorker(); await h.login();
  try {
    const job = (await h.req("/api/jobs", { method: "POST", body: { task: "noop", run_after: new Date(Date.now() + 1e7).toISOString() } })).data.id;
    const sch = (await h.req("/api/schedules", { method: "POST", body: { name: "s", task: "noop", kind: "cron", cron_expr: "0 9 * * *" } })).data.id;
    const llm = (await h.req("/api/settings/llm", { method: "POST", body: { name: "a", provider: "openai", model: "m", api_key: "k-123456789012" } })).data.id;
    const soc = (await h.req("/api/settings/social", { method: "POST", body: { platform: "x", secrets: { access_token: "t12345678" } } })).data.id;
    const calls = [[`/api/jobs/${job}/cancel`, "POST"], [`/api/jobs/${job}/retry`, "POST"], [`/api/schedules/${sch}/pause`, "POST"], [`/api/schedules/${sch}/resume`, "POST"], [`/api/schedules/${sch}/run-now`, "POST"],
      [`/api/schedules/${sch}/runs`, "GET"], [`/api/settings/llm/${llm}/default`, "POST"], [`/api/settings/social/${soc}`, "PUT", { platform: "x", name: "n", secrets: {} }], [`/api/settings/github`, "PUT", { owner: "a", repo: "b" }]];
    for (const [p, m, b] of calls) { const r = await h.req(p, { method: m, body: b }); assert.ok(r.status !== 404 && r.status !== 405, `${m} ${p} -> ${r.status}`); }
    for (const [p] of [[`/api/schedules/${sch}`], [`/api/settings/llm/${llm}`], [`/api/settings/social/${soc}`]]) assert.equal((await h.req(p, { method: "DELETE" })).status, 200);
  } finally { await h.dispose(); }
});
