// Executes the generated n8n workflow's own expressions/Code node against the real Worker (contract test).
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { makeWorker, SECRETS, waitFor } from "./harness.mjs";

const dir = new URL("../n8n-workflows/", import.meta.url);
const evalExpr = (expr, ctx) => { const inner = /\{\{([\s\S]*)\}\}/.exec(expr)[1]; return new Function("$json", "$", `return (${inner})`)(ctx.json, (name) => ({ item: { json: ctx.nodes[name] } })); };

for (const file of readdirSync(dir).filter((f) => f.endsWith(".json"))) test(`n8n workflow ${file} works end to end against the Worker`, async () => {
  const wf = JSON.parse(readFileSync(new URL(file, dir), "utf8")), platform = file.replace(".json", "");
  const node = (n) => wf.nodes.find((x) => x.name === n);
  const params = Object.fromEntries(node("Parameters").parameters.assignments.assignments.map((a) => [a.name, a.value]));
  const H = { "x-addon-token": SECRETS.N8N_ADDON_API_TOKEN };
  const h = await makeWorker({ outbound: async (req) => req.url.includes("deepseek") ? Response.json({ choices: [{ message: { content: "Fresh post about growing online. ".repeat(40) } }] })
      : req.url.includes("uploadType=resumable") ? new Response("{}", { headers: { location: "https://upload.google/s1" } })
      : req.url.includes("example.com") ? new Response(new Uint8Array(20), { headers: { "content-type": "video/mp4" } })
      : Response.json({ data: { id: "1" }, id: "1", access_token: "AT", json: { errors: [], data: { id: "r1" } } }) });
  try {
    await h.login();
    await h.req("/api/settings/llm", { method: "POST", body: { name: "DS", provider: "deepseek", model: "deepseek-chat", api_key: "KEY-123456789" } });
    const secrets = { x: { access_token: "T1234567890" }, threads: { user_id: "1", access_token: "T1234567890" }, linkedin: { access_token: "T1234567890", author_urn: "urn:li:person:1" },
      facebook: { page_id: "1", page_access_token: "T1234567890" }, instagram: { ig_user_id: "1", access_token: "T1234567890" }, pinterest: { access_token: "T1234567890", board_id: "1" },
      reddit: { client_id: "a", client_secret: "b", access_token: "T1234567890" }, youtube: { client_id: "a", client_secret: "b", refresh_token: "r" } }[platform];
    params.account_id = (await h.req("/api/settings/social", { method: "POST", body: { platform, secrets } })).data.id;
    // 1) Generate content (LLM) node
    const gen = node("Generate content (LLM)").parameters;
    const genBody = JSON.parse(evalExpr(gen.jsonBody, { json: params, nodes: {} }));
    const generated = await h.req("/api/n8n/generate", { method: "POST", body: genBody, headers: H });
    assert.equal(generated.status, 200);
    // 2) Format node (real Code node source)
    const out = new Function("$input", node(`Format for ${platform}`).parameters.jsCode)({ first: () => ({ json: generated.data }) })[0].json;
    const limit = { x: 280, threads: 500, linkedin: 3000, instagram: 2200, facebook: 63000, reddit: 40000, pinterest: 800, youtube: 5000 }[platform];
    assert.ok(out.text.length <= limit && out.text.length > 10);
    // 3) Queue publish job node
    const q = node("Queue publish job").parameters;
    const jobBody = JSON.parse(evalExpr(q.jsonBody, { json: out, nodes: { Parameters: params } }));
    assert.equal(jobBody.task, "social_publish");
    const created = await h.req("/api/n8n/jobs", { method: "POST", body: jobBody, headers: H });
    assert.equal(created.status, 200);
    // 4) Get job status node (the Worker's real route), polled like the Wait/IF loop
    const done = await waitFor(async () => ["completed", "failed"].includes((await h.req(`/api/n8n/jobs/${created.data.id}`, { headers: H })).data.status), 20000); if (!done) console.log("STUCK", platform, JSON.stringify((await h.req(`/api/n8n/jobs/${created.data.id}`, { headers: H })).data));
    const final = (await h.req(`/api/n8n/jobs/${created.data.id}`, { headers: H })).data;
    assert.equal(final.status, "completed", JSON.stringify(final));
    assert.equal(final.result.platform, platform);
  } finally { await h.dispose(); }
});
