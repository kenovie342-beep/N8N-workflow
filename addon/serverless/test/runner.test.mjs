import test from "node:test";
import assert from "node:assert/strict";
import { makeWorker, SECRETS } from "./harness.mjs";
import { encryptJSON } from "../src/crypto.js";
import * as Q from "../src/queue.js";

async function withGithub(h, token = "github_pat_TESTTOKEN0123456789abcdef") {
  await h.db.prepare("INSERT INTO settings (id, kind, name, config_json, secret_enc, created_at, updated_at) VALUES ('g','github','github',?,?,1,1)")
    .bind(JSON.stringify({ owner: "me", repo: "runner", workflow: "remote-job.yml", n8n_workflow: "n8n-host.yml", ref: "main" }), await encryptJSON(SECRETS.ENCRYPTION_KEY, { token })).run();
}
async function remoteJob(h, extra = {}) {
  const j = await Q.enqueue(h.db, { task: "github_job", payload: { task: "echo", data: 1 }, maxAttempts: 2, ...extra });
  await Q.claim(h.db, {}); return j.id;
}
const cb = (h, body, o) => h.signed("/api/runner/callback", JSON.stringify(body), o);

test("github_job: dispatch sends only job_id, token only in header, job stays running (holds a slot) until callback", async () => {
  const h = await makeWorker(); await h.login();
  try {
    await withGithub(h);
    const id = (await Q.enqueue(h.db, { task: "github_job", payload: { task: "echo", data: { big: "x".repeat(200) } } })).id;
    await h.tick();
    const call = h.calls.find((c) => c.url.endsWith("/actions/workflows/remote-job.yml/dispatches"));
    assert.ok(call);
    assert.deepEqual(JSON.parse(call.body), { ref: "main", inputs: { job_id: id } });
    assert.ok(!call.body.includes("github_pat_") && !call.body.includes("xxxx"));
    assert.equal(call.headers.authorization, "Bearer github_pat_TESTTOKEN0123456789abcdef");
    const row = (await h.q("SELECT status, executor, lease_expires_at FROM jobs WHERE id=?", id))[0];
    assert.equal(row.status, "running"); assert.equal(row.executor, "github"); assert.ok(row.lease_expires_at > Date.now() + 600e3);
  } finally { await h.dispose(); }
});

test("github_job: unconfigured / 422 fail permanently with a clear message; 503 is retried", async () => {
  let status = 422;
  const h = await makeWorker({ outbound: async () => new Response("nope", { status }) }); await h.login();
  try {
    const a = (await Q.enqueue(h.db, { task: "github_job" })).id;
    await h.tick();
    let r = (await h.q("SELECT status, error FROM jobs WHERE id=?", a))[0];
    assert.equal(r.status, "failed"); assert.match(r.error, /not configured/);
    await withGithub(h);
    const b = (await Q.enqueue(h.db, { task: "github_job", maxAttempts: 3 })).id;
    await h.tick();
    r = (await h.q("SELECT status, error FROM jobs WHERE id=?", b))[0];
    assert.equal(r.status, "failed"); assert.match(r.error, /422/);
    status = 503;
    const c = (await Q.enqueue(h.db, { task: "github_job", maxAttempts: 3 })).id;
    await h.tick();
    r = (await h.q("SELECT status FROM jobs WHERE id=?", c))[0];
    assert.equal(r.status, "queued");
    assert.ok(!JSON.stringify(await h.q("SELECT error FROM jobs")).includes("github_pat_"));
  } finally { await h.dispose(); }
});

test("callback: valid success completes job and stores logs", async () => {
  const h = await makeWorker();
  try {
    const id = await remoteJob(h);
    const r = await cb(h, { job_id: id, status: "completed", result: { echo: 1 }, logs: "line1\nline2" });
    assert.equal(r.status, 200); assert.equal(r.data.applied, true);
    assert.equal((await h.q("SELECT status FROM jobs WHERE id=?", id))[0].status, "completed");
    assert.deepEqual((await h.q("SELECT message FROM job_logs WHERE job_id=? ORDER BY id", id)).map((x) => x.message), ["line1", "line2"]);
  } finally { await h.dispose(); }
});

test("callback: bad/missing/stale signature and tampered body rejected, job untouched", async () => {
  const h = await makeWorker();
  try {
    const id = await remoteJob(h), body = { job_id: id, status: "completed" };
    assert.equal((await cb(h, body, { secret: "wrong" })).status, 401);
    assert.equal((await h.req("/api/runner/callback", { method: "POST", body })).status, 401);
    assert.equal((await cb(h, body, { ts: String(Math.floor(Date.now() / 1000) - 3600) })).status, 401);
    const text = JSON.stringify(body);
    const { signMessage } = await import("../src/crypto.js");
    const ts = String(Math.floor(Date.now() / 1000)), nonce = "n1", sig = await signMessage(SECRETS.CALLBACK_SECRET, ts, nonce, text);
    const tampered = await h.req("/api/runner/callback", { method: "POST", raw: text + " ", headers: { "x-suite-timestamp": ts, "x-suite-nonce": nonce, "x-suite-signature": sig } });
    assert.equal(tampered.status, 401);
    assert.equal((await h.q("SELECT status FROM jobs WHERE id=?", id))[0].status, "running");
  } finally { await h.dispose(); }
});

test("callback: replay is a no-op; duplicate with a new nonce is idempotent (first result wins)", async () => {
  const h = await makeWorker();
  try {
    const id = await remoteJob(h);
    assert.equal((await cb(h, { job_id: id, status: "completed", result: { v: 1 } }, { nonce: "fixed" })).data.applied, true);
    const replay = await cb(h, { job_id: id, status: "completed", result: { v: 1 } }, { nonce: "fixed" });
    assert.deepEqual([replay.data.duplicate, replay.data.applied], [true, false]);
    const dup = await cb(h, { job_id: id, status: "completed", result: { v: 999 } });
    assert.equal(dup.data.applied, false);
    assert.deepEqual(JSON.parse((await h.q("SELECT result_json FROM jobs WHERE id=?", id))[0].result_json), { v: 1 });
  } finally { await h.dispose(); }
});

test("callback: late callback cannot resurrect a cancelled job; failed callbacks retry or fail; validation", async () => {
  const h = await makeWorker();
  try {
    const id = await remoteJob(h);
    await h.db.prepare("UPDATE jobs SET status='cancelled' WHERE id=?").bind(id).run();
    assert.equal((await cb(h, { job_id: id, status: "completed" })).data.applied, false);
    assert.equal((await h.q("SELECT status FROM jobs WHERE id=?", id))[0].status, "cancelled");
    const a = await remoteJob(h);
    assert.equal((await cb(h, { job_id: a, status: "failed", error: "boom", retryable: true })).data.applied, true);
    assert.equal((await h.q("SELECT status FROM jobs WHERE id=?", a))[0].status, "queued");
    const b = await remoteJob(h, { maxAttempts: 1 });
    await cb(h, { job_id: b, status: "failed", error: "fatal" });
    assert.equal((await h.q("SELECT status FROM jobs WHERE id=?", b))[0].status, "failed");
    assert.equal((await cb(h, { status: "completed" })).status, 400);
    assert.equal((await cb(h, { job_id: "x", status: "weird" })).status, 400);
    assert.equal((await cb(h, { job_id: "does-not-exist", status: "completed" })).status, 404);
  } finally { await h.dispose(); }
});

test("runner payload endpoint: signed, single use, remote running jobs only", async () => {
  const h = await makeWorker();
  try {
    const id = await remoteJob(h);
    const ok = await h.signed(`/api/runner/jobs/${id}/payload`, id, { method: "GET" });
    assert.equal(ok.status, 200); assert.equal(ok.data.payload.task, "echo");
    assert.equal((await h.signed(`/api/runner/jobs/${id}/payload`, id, { method: "GET", nonce: "r" })).status, 200);
    assert.equal((await h.signed(`/api/runner/jobs/${id}/payload`, id, { method: "GET", nonce: "r" })).status, 409);
    assert.equal((await h.req(`/api/runner/jobs/${id}/payload`)).status, 401);
    const local = (await Q.enqueue(h.db, { task: "noop", runAfter: Date.now() - 1 })).id; await Q.claim(h.db, {});
    assert.equal((await h.signed(`/api/runner/jobs/${local}/payload`, local, { method: "GET" })).status, 404);
  } finally { await h.dispose(); }
});

test("cancel a running GitHub job finds the run by title and calls GitHub's cancel endpoint", async () => {
  let jobId = "";
  const h = await makeWorker({ outbound: async (req) => req.url.includes("/actions/runs?") ? Response.json({ workflow_runs: [{ id: 777, display_title: `suite-job ${jobId}` }] }) : new Response("{}", { status: 202 }) });
  await h.login();
  try {
    await withGithub(h);
    jobId = await remoteJob(h);
    const r = await h.req(`/api/jobs/${jobId}/cancel`, { method: "POST" });
    assert.equal(r.data.status, "cancelled");
    assert.ok(h.calls.some((c) => c.method === "POST" && c.url.endsWith("/actions/runs/777/cancel")), "GitHub cancel was not called");
    assert.equal((await h.q("SELECT status FROM jobs WHERE id=?", jobId))[0].status, "cancelled");
  } finally { await h.dispose(); }
});

test("settings can never leak the GitHub token", async () => {
  const h = await makeWorker(); await h.login();
  try {
    await withGithub(h, "github_pat_TOPSECRETVALUE0123456789");
    const all = JSON.stringify([(await h.req("/api/jobs")).data, (await h.req("/api/stats")).data, (await h.req("/api/settings/app")).data]);
    assert.ok(!all.includes("TOPSECRETVALUE"));
  } finally { await h.dispose(); }
});
