import test from "node:test";
import assert from "node:assert/strict";
import { makeWorker } from "./harness.mjs";
import { nextCron, parseCron, localToUtc } from "../src/cron.js";
import * as S from "../src/scheduler.js";

const utc = (y, mo, d, h = 0, mi = 0) => Date.UTC(y, mo - 1, d, h, mi);

test("cron: Lagos is UTC+1 (09:00 Lagos = 08:00 UTC), rolls to next day", () => {
  assert.equal(nextCron("0 9 * * *", "Africa/Lagos", utc(2026, 11, 13, 0, 0)), utc(2026, 11, 13, 8, 0));
  assert.equal(nextCron("0 9 * * *", "Africa/Lagos", utc(2026, 11, 13, 8, 0)), utc(2026, 11, 14, 8, 0));
});
test("cron: DST zones (New York) use the right offset in winter and summer", () => {
  assert.equal(nextCron("0 9 * * *", "America/New_York", utc(2026, 11, 13)), utc(2026, 11, 13, 14, 0));   // EST UTC-5
  assert.equal(nextCron("0 9 * * *", "America/New_York", utc(2026, 7, 13)), utc(2026, 7, 13, 13, 0));     // EDT UTC-4
});
test("cron: steps, lists, ranges, names, weekdays, dom/dow OR rule", () => {
  assert.equal(nextCron("*/15 * * * *", "UTC", utc(2026, 1, 1, 10, 1)), utc(2026, 1, 1, 10, 15));
  assert.equal(nextCron("30 6 * * mon-fri", "UTC", utc(2026, 11, 14, 0, 0)), utc(2026, 11, 16, 6, 30));        // Sat -> Mon
  assert.equal(nextCron("0 0 1,15 * *", "UTC", utc(2026, 1, 2)), utc(2026, 1, 15));
  assert.equal(nextCron("0 0 1 jan,jul *", "UTC", utc(2026, 2, 1)), utc(2026, 7, 1));
  assert.equal(nextCron("0 12 13 * fri", "UTC", utc(2026, 11, 1)), utc(2026, 11, 6, 12));                         // dom OR dow: first Friday wins
  assert.deepEqual(parseCron("0 0 * * 7").dow, new Set([0]));
});
test("cron: invalid expressions and timezones rejected", () => {
  for (const bad of ["", "* * * *", "61 * * * *", "* 24 * * *", "*/0 * * * *", "a b c d e", "5-1 * * * *"]) assert.throws(() => parseCron(bad), bad);
  assert.throws(() => nextCron("0 9 * * *", "Mars/Base", Date.now()), /timezone/i);
});
test("cron: nonexistent DST-gap local time is skipped, not mis-fired", () => {
  const n = nextCron("30 2 * * *", "America/New_York", utc(2026, 3, 7, 12));     // 2026-03-08 02:30 does not exist
  assert.notEqual(n, localToUtc("America/New_York", 2026, 3, 8, 2, 30));
  assert.ok(n >= utc(2026, 3, 9, 6));
});

const mk = (h, extra = {}) => h.req("/api/schedules", { method: "POST", body: { name: "s", task: "noop", kind: "cron", cron_expr: "0 9 * * *", ...extra } });

test("API: default timezone Africa/Lagos; create/edit/pause/resume/delete", async () => {
  const h = await makeWorker(); await h.login();
  try {
    assert.equal((await h.req("/api/settings/app")).data.default_timezone, "Africa/Lagos");
    const s = (await mk(h)).data; assert.equal(s.timezone, "Africa/Lagos"); assert.ok(s.next_run_at && s.enabled);
    assert.equal(new Date(s.next_run_at).getUTCHours(), 8);                                  // 09:00 Lagos
    const p = (await h.req(`/api/schedules/${s.id}/pause`, { method: "POST" })).data; assert.equal(p.enabled, false); assert.equal(p.next_run_at, null);
    const r = (await h.req(`/api/schedules/${s.id}/resume`, { method: "POST" })).data; assert.ok(r.enabled && r.next_run_at);
    const e = (await h.req(`/api/schedules/${s.id}`, { method: "PUT", body: { name: "renamed", cron_expr: "30 6 * * 1", timezone: "Europe/London" } })).data;
    assert.deepEqual([e.name, e.cron_expr, e.timezone], ["renamed", "30 6 * * 1", "Europe/London"]);
    assert.equal((await h.req("/api/schedules")).data.length, 1);
    assert.equal((await h.req(`/api/schedules/${s.id}`, { method: "DELETE" })).status, 200);
    assert.deepEqual((await h.req("/api/schedules")).data, []);
    assert.equal((await h.req(`/api/schedules/${s.id}`, { method: "PUT", body: { name: "x" } })).status, 404);
  } finally { await h.dispose(); }
});

test("API: validation errors", async () => {
  const h = await makeWorker(); await h.login();
  try {
    for (const bad of [{ cron_expr: "nonsense" }, { kind: "interval", interval_seconds: 5 }, { timezone: "Mars/Base" }, { task: "rm_rf" }, { kind: "once" }, { kind: "weird" }])
      assert.equal((await mk(h, bad)).status, 400, JSON.stringify(bad));
  } finally { await h.dispose(); }
});

test("one-time schedule: local time converted to UTC; fires once then disables", async () => {
  const h = await makeWorker(); await h.login();
  try {
    const local = new Date(Date.now() + 2 * 3600e3 + 3600e3).toISOString().slice(0, 16);   // "local Lagos" = UTC+1
    const s = (await mk(h, { kind: "once", cron_expr: "", run_at: local })).data;
    assert.ok(Math.abs(s.next_run_at - (Date.now() + 2 * 3600e3)) < 120e3);
    await h.db.prepare("UPDATE schedules SET next_run_at = ? WHERE id = ?").bind(Date.now() - 1000, s.id).run();
    assert.equal(await S.tick(h.db), 1);
    const row = (await h.q("SELECT enabled, next_run_at FROM schedules WHERE id=?", s.id))[0];
    assert.deepEqual([row.enabled, row.next_run_at], [0, null]);
    assert.equal(await S.tick(h.db), 0);
  } finally { await h.dispose(); }
});

test("tick: exactly once per slot, advances, paused never fires", async () => {
  const h = await makeWorker(); await h.login();
  try {
    const s = (await mk(h, { kind: "interval", cron_expr: "", interval_seconds: 60 })).data;
    await h.db.prepare("UPDATE schedules SET next_run_at = ? WHERE id=?").bind(Date.now() - 5000, s.id).run();
    assert.equal(await S.tick(h.db), 1); assert.equal(await S.tick(h.db), 0);
    const row = (await h.q("SELECT next_run_at, last_run_at FROM schedules WHERE id=?", s.id))[0];
    assert.ok(row.next_run_at > Date.now() && row.last_run_at);
    assert.equal((await h.q("SELECT * FROM jobs WHERE schedule_id=?", s.id)).length, 1);
    await h.req(`/api/schedules/${s.id}/pause`, { method: "POST" });
    await h.db.prepare("UPDATE schedules SET next_run_at = ? WHERE id=?").bind(Date.now() - 5000, s.id).run();
    assert.equal(await S.tick(h.db), 0);
  } finally { await h.dispose(); }
});

test("tick: long downtime skips to the next slot instead of flooding", async () => {
  const h = await makeWorker(); await h.login();
  try {
    const s = (await mk(h, { kind: "interval", cron_expr: "", interval_seconds: 60 })).data;
    await h.db.prepare("UPDATE schedules SET next_run_at = ? WHERE id=?").bind(Date.now() - 3 * 86400e3, s.id).run();
    assert.equal(await S.tick(h.db), 0);
    assert.ok((await h.q("SELECT next_run_at FROM schedules WHERE id=?", s.id))[0].next_run_at > Date.now());
  } finally { await h.dispose(); }
});

test("schedules survive a runtime restart and an overdue one fires once (verified end to end through the cron handler)", async () => {
  const h = await makeWorker(); await h.login(); const persist = h.persist;
  const s = (await mk(h, { kind: "interval", cron_expr: "", interval_seconds: 3600 })).data;
  await h.db.prepare("UPDATE schedules SET next_run_at = ? WHERE id=?").bind(Date.now() - 600e3, s.id).run();   // overdue by 10 minutes
  await h.dispose();
  const h2 = await makeWorker({ persist }); await h2.login();
  try {
    assert.equal((await h2.req("/api/schedules")).data.length, 1);
    await h2.tick();                                                    // real scheduled() entry point: schedule tick + pump
    const runs = (await h2.req(`/api/schedules/${s.id}/runs`)).data;
    assert.equal(runs.length, 1); assert.equal(runs[0].status, "completed");
    await h2.tick();
    assert.equal((await h2.req(`/api/schedules/${s.id}/runs`)).data.length, 1);
  } finally { await h2.dispose(); }
});

test("upcoming executions and run-now/history", async () => {
  const h = await makeWorker(); await h.login();
  try {
    const s = (await mk(h)).data;
    const up = (await h.req("/api/schedules/upcoming")).data;
    assert.equal(up.length, 3); assert.equal(up[0].timezone, "Africa/Lagos"); assert.ok(up[0].at_utc < up[1].at_utc);
    await h.req(`/api/schedules/${s.id}/run-now`, { method: "POST" });
    assert.equal((await h.req(`/api/schedules/${s.id}/runs`)).data.length, 1);
  } finally { await h.dispose(); }
});
