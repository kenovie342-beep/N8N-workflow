import test from "node:test";
import assert from "node:assert/strict";
import { makeWorker, waitFor } from "./harness.mjs";
import * as Q from "../src/queue.js";

async function sampleWhile(h, fn) {
  let stop = false, peakRunning = 0, queuedWhile5 = false;
  const sampler = (async () => { while (!stop) { const r = await h.count("running"), q = await h.count("queued"); peakRunning = Math.max(peakRunning, r); if (r === 5 && q > 0) queuedWhile5 = true; await new Promise((x) => setTimeout(x, 15)); } })();
  await fn(); stop = true; await sampler;
  return { peakRunning, queuedWhile5 };
}

test("limit 5, 12 jobs, 6 OVERLAPPING Worker invocations: never >5 running; rest stay queued; all finish", async () => {
  const h = await makeWorker(); await h.login();
  try {
    assert.equal((await h.req("/api/settings/app", { method: "PUT", body: { max_concurrency: 5 } })).data.max_concurrency, 5);
    for (let i = 0; i < 12; i++) await Q.enqueue(h.db, { task: "sleep", payload: { ms: 700 } });
    const { peakRunning, queuedWhile5 } = await sampleWhile(h, async () => {
      for (let round = 0; round < 12 && (await h.count("completed")) < 12; round++) await Promise.all(Array.from({ length: 6 }, () => h.tick()));
    });
    assert.equal(await h.count("completed"), 12, "jobs were lost or never ran");
    assert.ok(peakRunning <= 5, `DB-observed running peaked at ${peakRunning}`);
    assert.ok(queuedWhile5, "5 should run while others wait queued");
    const peaks = (await h.q("SELECT result_json FROM jobs")).map((r) => JSON.parse(r.result_json).peak_in_isolate);
    assert.equal(Math.max(...peaks), 5, "in-isolate parallelism must reach but never exceed 5");
  } finally { await h.dispose(); }
});

for (const limit of [10, 20]) test(`limit ${limit} is enforced with ${limit * 2} jobs`, async () => {
  const h = await makeWorker({ vars: { MAX_JOBS_PER_TICK: "50" } }); await h.login();
  try {
    await h.req("/api/settings/app", { method: "PUT", body: { max_concurrency: limit } });
    for (let i = 0; i < limit * 2; i++) await Q.enqueue(h.db, { task: "sleep", payload: { ms: 600 } });
    let peak = 0, stop = false;
    const s = (async () => { while (!stop) { peak = Math.max(peak, await h.count("running")); await new Promise((r) => setTimeout(r, 15)); } })();
    for (let round = 0; round < 10 && (await h.count("completed")) < limit * 2; round++) await Promise.all(Array.from({ length: 8 }, () => h.tick()));
    stop = true; await s;
    assert.equal(await h.count("completed"), limit * 2);
    assert.ok(peak <= limit && peak >= limit - 1, `peak ${peak} vs limit ${limit}`);
  } finally { await h.dispose(); }
});

test("changing the limit at runtime applies on the next claim", async () => {
  const h = await makeWorker(); await h.login();
  try {
    for (let i = 0; i < 8; i++) await Q.enqueue(h.db, { task: "noop" });
    await h.req("/api/settings/app", { method: "PUT", body: { max_concurrency: 2 } });
    assert.equal((await Q.claim(h.db, {})).length, 2);
    await h.req("/api/settings/app", { method: "PUT", body: { max_concurrency: 4 } });
    assert.equal((await Q.claim(h.db, {})).length, 2);   // 4 total running now
    assert.equal(await h.count("running"), 4);
  } finally { await h.dispose(); }
});

test("invalid concurrency values rejected", async () => {
  const h = await makeWorker(); await h.login();
  try {
    for (const v of [0, 101, "abc", 2.5]) assert.equal((await h.req("/api/settings/app", { method: "PUT", body: { max_concurrency: v } })).status, 400);
  } finally { await h.dispose(); }
});

test("priority then FIFO order", async () => {
  const h = await makeWorker();
  try {
    await h.req("/api/settings/app", {}); await h.db.prepare("INSERT INTO app_settings VALUES ('max_concurrency','1')").run();
    const a = await Q.enqueue(h.db, { task: "noop", priority: 1 }); await new Promise((r) => setTimeout(r, 5));
    const b = await Q.enqueue(h.db, { task: "noop", priority: 9 }); await new Promise((r) => setTimeout(r, 5));
    const c = await Q.enqueue(h.db, { task: "noop", priority: 9 });
    assert.equal((await Q.claim(h.db, {}))[0].id, b.id);
    await Q.complete(h.db, b.id);
    assert.equal((await Q.claim(h.db, {}))[0].id, c.id);
    await Q.complete(h.db, c.id);
    assert.equal((await Q.claim(h.db, {}))[0].id, a.id);
  } finally { await h.dispose(); }
});

test("duplicate prevention applies to active jobs only", async () => {
  const h = await makeWorker();
  try {
    const j1 = await Q.enqueue(h.db, { task: "noop", dedupeKey: "k" }), j2 = await Q.enqueue(h.db, { task: "noop", dedupeKey: "k" });
    assert.ok(j1.created && !j2.created && j1.id === j2.id);
    await Q.claim(h.db, {}); await Q.complete(h.db, j1.id);
    assert.ok((await Q.enqueue(h.db, { task: "noop", dedupeKey: "k" })).created);
  } finally { await h.dispose(); }
});

test("retry with exponential backoff, Retry-After honoured, then final failure", async () => {
  const h = await makeWorker();
  try {
    const j = await Q.enqueue(h.db, { task: "noop", maxAttempts: 2 });
    await Q.claim(h.db, {});
    assert.equal(await Q.fail(h.db, j.id, "boom"), "queued");
    let row = (await h.q("SELECT run_after FROM jobs WHERE id=?", j.id))[0];
    assert.ok(row.run_after > Date.now() + 3000, "backoff applied");
    assert.equal((await Q.claim(h.db, {})).length, 0, "not runnable before backoff");
    await h.db.prepare("UPDATE jobs SET run_after = 0 WHERE id=?").bind(j.id).run();
    assert.equal((await Q.claim(h.db, {})).length, 1);
    assert.equal(await Q.fail(h.db, j.id, "boom again"), "failed");

    const k = await Q.enqueue(h.db, { task: "noop", maxAttempts: 3 });
    await Q.claim(h.db, {}); await Q.fail(h.db, k.id, "429", { retryAfter: 300 });
    row = (await h.q("SELECT run_after FROM jobs WHERE id=?", k.id))[0];
    assert.ok(row.run_after > Date.now() + 290000);
  } finally { await h.dispose(); }
});

test("worker-run failures: permanent errors fail at once, transient errors are retried", async () => {
  const h = await makeWorker(); await h.login();
  try {
    const bad = (await h.req("/api/jobs", { method: "POST", body: { task: "sleep", payload: { ms: 10, fail: "boom" }, max_attempts: 3 } })).data.id;
    await waitFor(async () => (await h.q("SELECT status FROM jobs WHERE id=?", bad))[0].status !== "running", 5000);
    const r = (await h.q("SELECT status, attempts, error FROM jobs WHERE id=?", bad))[0];
    assert.equal(r.status, "queued"); assert.equal(r.attempts, 1); assert.match(r.error, /boom/);   // generic errors are retried (bounded by max_attempts)
    const unk = await Q.enqueue(h.db, { task: "does_not_exist" });
    await h.tick();
    const u = (await h.q("SELECT status, attempts FROM jobs WHERE id=?", unk.id))[0];
    assert.equal(u.status, "failed"); assert.equal(u.attempts, 1);                                   // unknown task is permanent
  } finally { await h.dispose(); }
});

test("cancel queued job; cancel flag for running; retry failed/cancelled", async () => {
  const h = await makeWorker(); await h.login();
  try {
    await h.db.prepare("INSERT INTO app_settings VALUES ('max_concurrency','1')").run();
    const a = await Q.enqueue(h.db, { task: "noop" }), b = await Q.enqueue(h.db, { task: "noop" });
    await Q.claim(h.db, {});
    assert.equal((await h.req(`/api/jobs/${b.id}/cancel`, { method: "POST" })).data.status, "cancelled");
    assert.equal((await h.req(`/api/jobs/${a.id}/cancel`, { method: "POST" })).data.status, "running");
    assert.equal((await h.q("SELECT cancel_requested AS c FROM jobs WHERE id=?", a.id))[0].c, 1);
    assert.equal((await h.req(`/api/jobs/${b.id}/retry`, { method: "POST" })).data.status, "queued");
    assert.equal((await h.req(`/api/jobs/${a.id}/retry`, { method: "POST" })).status, 409);
  } finally { await h.dispose(); }
});

test("interrupted (lease-expired) jobs are recovered, never lost", async () => {
  const h = await makeWorker();
  try {
    const j = await Q.enqueue(h.db, { task: "noop", maxAttempts: 3 });
    await Q.claim(h.db, {});                                 // "isolate dies" here
    await h.db.prepare("UPDATE jobs SET lease_expires_at = ? WHERE id=?").bind(Date.now() - 1000, j.id).run();
    assert.equal(await Q.recoverExpired(h.db), 1);
    assert.equal((await h.q("SELECT status FROM jobs WHERE id=?", j.id))[0].status, "queued");
    const k = await Q.enqueue(h.db, { task: "noop", maxAttempts: 1 });
    await h.db.prepare("UPDATE jobs SET run_after=0 WHERE id=?").bind(j.id).run();
    await Q.claim(h.db, { perTick: 1 }); await h.db.prepare("UPDATE jobs SET lease_expires_at = 1 WHERE status='running'").run();
    await Q.recoverExpired(h.db);
    assert.ok((await h.q("SELECT 1 FROM jobs WHERE id = ?", k.id)).length === 1);
  } finally { await h.dispose(); }
});

test("jobs and state persist across a full restart of the runtime", async () => {
  const h = await makeWorker(); await h.login();
  const persist = h.persist;
  const id = (await h.req("/api/jobs", { method: "POST", body: { task: "noop", run_after: new Date(Date.now() + 3600e3).toISOString(), payload: { keep: "me" } } })).data.id;
  await h.req("/api/settings/app", { method: "PUT", body: { max_concurrency: 7 } });
  await h.dispose();
  const h2 = await makeWorker({ persist }); await h2.login();
  try {
    const j = await h2.req(`/api/jobs/${id}`);
    assert.equal(j.data.status, "queued"); assert.equal(j.data.payload.keep, "me");
    assert.equal((await h2.req("/api/settings/app")).data.max_concurrency, 7);
  } finally { await h2.dispose(); }
});

test("stats come from real job rows", async () => {
  const h = await makeWorker(); await h.login();
  try {
    assert.equal((await h.req("/api/stats")).data.total, 0);
    for (let i = 0; i < 3; i++) await Q.enqueue(h.db, { task: "noop", runAfter: Date.now() + 1e7 });
    await h.db.prepare("UPDATE jobs SET status='running' WHERE id = (SELECT id FROM jobs LIMIT 1)").run();
    const s = (await h.req("/api/stats")).data;
    assert.deepEqual([s.total, s.running, s.queued], [3, 1, 2]);
  } finally { await h.dispose(); }
});
