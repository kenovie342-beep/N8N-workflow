import test from "node:test";
import assert from "node:assert/strict";
import { makeWorker, SECRETS } from "./harness.mjs";
import { activeWindow, decide, parseWindow, watchdog, recordHeartbeat, n8nStatus, LEAD_MS, BEAT_STALE_MS, STARTUP_GRACE_MS } from "../src/windows.js";

const lagos = (y, mo, d, h, mi) => Date.UTC(y, mo - 1, d, h - 1, mi);     // Lagos = UTC+1
const W = ["08:20-10:00", "13:20-15:00", "18:20-20:00"];

test("window parsing", () => {
  assert.deepEqual(parseWindow("08:20-10:00"), { sh: 8, sm: 20, eh: 10, em: 0 });
  for (const bad of ["8-10", "25:00-26:00", "10:00-09:00", "08:20", "08:20-08:20"]) assert.throws(() => parseWindow(bad), bad);
});
test("activeWindow: Lagos time, includes the 10-minute lead, excludes the end", () => {
  const at = (h, m) => activeWindow(W, "Africa/Lagos", lagos(2026, 11, 18, h, m));
  assert.equal(at(8, 9), null);
  assert.equal(at(8, 10)?.label, "08:20-10:00");          // lead start
  assert.equal(at(9, 59)?.label, "08:20-10:00");
  assert.equal(at(10, 0), null);
  assert.equal(at(12, 0), null);
  assert.equal(at(13, 15)?.label, "13:20-15:00");
  assert.equal(at(19, 30)?.label, "18:20-20:00");
  assert.equal(at(23, 0), null);
  assert.equal(new Date(at(9, 0).end).getUTCHours(), 9);   // 10:00 Lagos = 09:00 UTC
});
test("decide(): start / wait / none", () => {
  const win = { end: 1 }, n = 10_000_000;
  assert.equal(decide({ win: null, lastBeat: null, lastDispatch: null, nowMs: n }), "none");
  assert.equal(decide({ win, lastBeat: null, lastDispatch: null, nowMs: n }), "start");
  assert.equal(decide({ win, lastBeat: n - 1000, lastDispatch: null, nowMs: n }), "none");
  assert.equal(decide({ win, lastBeat: null, lastDispatch: n - 1000, nowMs: n }), "wait");
  assert.equal(decide({ win, lastBeat: n - BEAT_STALE_MS - 1, lastDispatch: n - STARTUP_GRACE_MS - 1, nowMs: n }), "start");
});

test("watchdog: starts n8n in a window, waits while booting, stays quiet while healthy, restarts if it dies, never outside windows", async () => {
  const h = await makeWorker();
  try {
    const sent = [], dispatch = async (_cfg, end) => { sent.push(end); };
    const env = SECRETS, t0 = lagos(2026, 11, 18, 8, 12);
    await h.db.prepare("INSERT INTO app_settings VALUES ('n8n_windows', ?)").bind(JSON.stringify(W)).run();
    assert.equal((await watchdog(env, h.db, { nowMs: lagos(2026, 11, 18, 7, 0), dispatch })).action, "none");       // before the lead time
    assert.equal((await watchdog(env, h.db, { nowMs: t0, dispatch })).action, "started");
    assert.equal(sent.length, 1); assert.equal(sent[0], new Date(lagos(2026, 11, 18, 10, 0)).toISOString());
    assert.equal((await watchdog(env, h.db, { nowMs: t0 + 60e3, dispatch })).action, "wait");                      // still booting
    await recordHeartbeat(h.db, { run_id: "123", tunnel_url: "https://x.trycloudflare.com" }, t0 + 120e3);
    assert.equal((await watchdog(env, h.db, { nowMs: t0 + 150e3, dispatch })).action, "none");                      // healthy
    assert.equal(sent.length, 1);
    const later = t0 + 120e3 + BEAT_STALE_MS + STARTUP_GRACE_MS + 60e3;                                             // heartbeat stopped
    assert.equal((await watchdog(env, h.db, { nowMs: later, dispatch })).action, "started");
    assert.equal(sent.length, 2);
    assert.equal((await watchdog(env, h.db, { nowMs: lagos(2026, 11, 18, 12, 0), dispatch })).action, "none");      // between windows
    assert.equal(sent.length, 2);
    const st = await n8nStatus(h.db, env, later);
    assert.equal(st.in_window, true); assert.equal(st.up, false);
  } finally { await h.dispose(); }
});

test("watchdog: dispatch failure is reported and retried next tick; disabled switch respected", async () => {
  const h = await makeWorker();
  try {
    const t0 = lagos(2026, 11, 18, 13, 25);
    const r = await watchdog(SECRETS, h.db, { nowMs: t0, dispatch: async () => { throw new Error("GitHub HTTP 503"); } });
    assert.equal(r.action, "dispatch_failed");
    const r2 = await watchdog(SECRETS, h.db, { nowMs: t0 + 60e3, dispatch: async () => {} });
    assert.equal(r2.action, "started");                              // failed dispatch did not start the grace period
    await h.db.prepare("INSERT INTO app_settings VALUES ('n8n_enabled','0')").run();
    assert.equal((await watchdog(SECRETS, h.db, { nowMs: t0 + 3600e3, dispatch: async () => assert.fail("must not dispatch") })).action, "disabled");
  } finally { await h.dispose(); }
});

test("settings API: validates windows; heartbeat endpoint is signed and replay-protected", async () => {
  const h = await makeWorker(); await h.login();
  try {
    assert.equal((await h.req("/api/settings/app", { method: "PUT", body: { n8n_windows: ["bad"] } })).status, 400);
    assert.equal((await h.req("/api/settings/app", { method: "PUT", body: { n8n_windows: ["07:00-08:00", "12:00-13:30"] } })).status, 200);
    assert.deepEqual((await h.req("/api/settings/app")).data.n8n_windows, ["07:00-08:00", "12:00-13:30"]);
    const body = JSON.stringify({ run_id: "9", tunnel_url: "https://t.trycloudflare.com" });
    assert.equal((await h.signed("/api/n8n/heartbeat", body, { secret: "wrong" })).status, 401);
    assert.equal((await h.signed("/api/n8n/heartbeat", body, { nonce: "same" })).status, 200);
    const st = (await h.req("/api/n8n/status")).data;
    assert.equal(st.up, true); assert.equal(st.run_id, "9");
  } finally { await h.dispose(); }
});

test("cron handler dispatches n8n to GitHub inside a window (mocked GitHub, real Worker)", async () => {
  const h = await makeWorker();
  try {
    await h.login();
    const now = new Date(), lagosH = (now.getUTCHours() + 1) % 24, m = now.getUTCMinutes();
    const start = `${String(lagosH).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
    const endTotal = Math.min(lagosH * 60 + m + 60, 23 * 60 + 59);
    if (lagosH * 60 + m + 60 > 23 * 60 + 59) return;                         // too close to midnight to build a same-day window
    const end = `${String(Math.floor(endTotal / 60)).padStart(2, "0")}:${String(endTotal % 60).padStart(2, "0")}`;
    await h.req("/api/settings/app", { method: "PUT", body: { n8n_windows: [`${start}-${end}`] } });
    await h.req("/api/settings/github", { method: "PUT", body: {} }).catch(() => {});
    const cfg = JSON.stringify({ owner: "me", repo: "runner", workflow: "remote-job.yml", n8n_workflow: "n8n-host.yml", ref: "main" });
    const { encryptJSON } = await import("../src/crypto.js");
    await h.db.prepare("INSERT INTO settings (id, kind, name, config_json, secret_enc, created_at, updated_at) VALUES ('g','github','github',?,?,1,1)").bind(cfg, await encryptJSON(SECRETS.ENCRYPTION_KEY, { token: "github_pat_TESTTOKEN0123456789abcdef" })).run();
    await h.tick();
    const call = h.calls.find((c) => c.url.includes("/actions/workflows/n8n-host.yml/dispatches"));
    assert.ok(call, "GitHub dispatch was not called");
    assert.equal(call.headers.authorization, "Bearer github_pat_TESTTOKEN0123456789abcdef");
    const sentBody = JSON.parse(call.body);
    assert.deepEqual(Object.keys(sentBody.inputs), ["window_end"]);          // only a timestamp is sent as an input
    assert.ok(!call.body.includes("github_pat_"));
  } finally { await h.dispose(); }
});
