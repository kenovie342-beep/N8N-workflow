import { HttpError, json, now, parseJSON } from "./util.js";
import { decryptJSON, encryptJSON, makeSession, maskMap, readSession, safeEqual, scrub, secretsEqual, signMessage } from "./crypto.js";
import { FINAL, addLog, cancel, complete, enqueue, fail, jobView, retry, stats } from "./queue.js";
import { cancelRun, findRun, githubConfig, runLogsUrl, runTitle } from "./github.js";
import * as sched from "./scheduler.js";
import { USER_TASKS, pump } from "./tasks.js";
import { getApp, getMaxConcurrency, getTimezone, getWindows, setApp } from "./settings.js";
import { n8nStatus, parseWindow, recordHeartbeat, watchdog } from "./windows.js";
import { assertTimezone } from "./cron.js";
import { dashboardResponse } from "./ui.js";

const SKEW_S = 300;

// ------------------------------------------------------------------ helpers
async function body(req) {
  const text = await req.text();
  if (text.length > 256_000) throw new HttpError(413, "Request too large");
  try { return text ? JSON.parse(text) : {}; } catch { throw new HttpError(400, "Invalid JSON"); }
}
async function rateLimit(db, key, max, windowS = 60) {
  const t = now(), start = t - (t % (windowS * 1000));
  await db.prepare("INSERT INTO rate_limits (k, window_start, hits) VALUES (?, ?, 1) ON CONFLICT(k) DO UPDATE SET hits = CASE WHEN window_start = excluded.window_start THEN hits + 1 ELSE 1 END, window_start = excluded.window_start").bind(key, start).run();
  const r = await db.prepare("SELECT hits FROM rate_limits WHERE k = ?").bind(key).first();
  if (r.hits > max) throw new HttpError(429, "Rate limit exceeded");
}
const clientIp = (req) => req.headers.get("cf-connecting-ip") || "unknown";

function cookie(req, name) {
  const m = (req.headers.get("cookie") || "").split(/;\s*/).find((c) => c.startsWith(name + "="));
  return m ? m.slice(name.length + 1) : null;
}
async function requireAdmin(req, env) {
  const s = await readSession(env.SESSION_SECRET, cookie(req, "suite_session"));
  if (!s) throw new HttpError(401, "Authentication required");
  if (!["GET", "HEAD", "OPTIONS"].includes(req.method) && !safeEqual(req.headers.get("x-csrf-token") || "", s.csrf)) throw new HttpError(403, "Missing or invalid CSRF token");
  return s;
}
async function requireN8nToken(req, env) {
  if (!env.N8N_ADDON_API_TOKEN || !(await secretsEqual(req.headers.get("x-addon-token") || "", env.N8N_ADDON_API_TOKEN))) throw new HttpError(401, "Invalid add-on token");
}
/** HMAC-signed machine request (GitHub runner / n8n host). Returns {nonce}. Replay-protected through D1. */
async function verifySigned(req, env, db, text) {
  const ts = req.headers.get("x-suite-timestamp") || "", nonce = req.headers.get("x-suite-nonce") || "", sig = req.headers.get("x-suite-signature") || "";
  if (!env.CALLBACK_SECRET) throw new HttpError(401, "Signing secret not configured");
  if (!ts || !nonce || !sig) throw new HttpError(401, "Missing signature headers");
  if (!/^\d+$/.test(ts) || Math.abs(Date.now() / 1000 - Number(ts)) > SKEW_S) throw new HttpError(401, "Stale request");
  if (!safeEqual(sig, await signMessage(env.CALLBACK_SECRET, ts, nonce, text))) throw new HttpError(401, "Invalid signature");
  await db.prepare("DELETE FROM nonces WHERE ts < ?").bind(now() - SKEW_S * 2000).run();
  const r = await db.prepare("INSERT OR IGNORE INTO nonces (nonce, ts) VALUES (?, ?)").bind(nonce, now()).run();
  return { nonce, replay: r.meta.changes === 0 };
}

// ------------------------------------------------------------------- router
async function route(req, env, ctx) {
  const db = env.DB, url = new URL(req.url), path = url.pathname, m = req.method;
  let g;

  if (path === "/health" && m === "GET") { await db.prepare("SELECT 1").first(); return json({ ok: true, service: "suite", db: true }); }
  if (path === "/" && m === "GET") return dashboardResponse();

  // ---- auth
  if (path === "/api/auth/login" && m === "POST") {
    await rateLimit(db, `login:${clientIp(req)}`, 10);
    const b = await body(req);
    const userOk = await secretsEqual(String(b.username || ""), env.ADMIN_USERNAME || "admin", "u");
    const passOk = await secretsEqual(String(b.password || ""), env.ADMIN_PASSWORD || "\u0000never", "p");
    if (!userOk || !passOk || !env.ADMIN_PASSWORD) throw new HttpError(401, "Invalid credentials");
    const { token, csrf } = await makeSession(env.SESSION_SECRET, String(b.username));
    return json({ ok: true, csrf }, 200, { "set-cookie": `suite_session=${token}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=43200` });
  }
  if (path === "/api/auth/logout" && m === "POST") return json({ ok: true }, 200, { "set-cookie": "suite_session=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0" });
  if (path === "/api/auth/me" && m === "GET") { const s = await requireAdmin(req, env); return json({ user: s.u, csrf: s.csrf }); }

  // ---- signed machine endpoints
  if (path === "/api/runner/callback" && m === "POST") {
    await rateLimit(db, `signed:${clientIp(req)}`, 300);
    const text = await req.text();
    const { replay } = await verifySigned(req, env, db, text);
    if (replay) return json({ ok: true, duplicate: true, applied: false });
    const d = parseJSON(text, null);
    if (!d || !d.job_id || !["completed", "failed"].includes(d.status)) throw new HttpError(400, "Invalid callback body");
    const job = await db.prepare("SELECT id FROM jobs WHERE id = ?").bind(d.job_id).first();
    if (!job) throw new HttpError(404, "Job not found");
    for (const line of String(d.logs || "").slice(0, 20000).split("\n").slice(0, 200)) if (line) await addLog(db, d.job_id, line);
    const applied = d.status === "completed" ? await complete(db, d.job_id, d.result || {}) : (await fail(db, d.job_id, String(d.error || "Runner reported failure"), { retryable: !!d.retryable })) !== null;
    return json({ ok: true, duplicate: !applied, applied });
  }
  if ((g = path.match(/^\/api\/runner\/jobs\/([\w-]+)\/payload$/)) && m === "GET") {
    await rateLimit(db, `signed:${clientIp(req)}`, 300);
    const { replay } = await verifySigned(req, env, db, g[1]);
    if (replay) throw new HttpError(409, "Replayed request");
    const j = await db.prepare("SELECT payload_json, executor, status FROM jobs WHERE id = ?").bind(g[1]).first();
    if (!j || j.executor !== "github" || j.status !== "running") throw new HttpError(404, "No running remote job with that id");
    return json({ job_id: g[1], payload: JSON.parse(j.payload_json) });
  }
  if (path === "/api/n8n/heartbeat" && m === "POST") {
    const text = await req.text();
    const { replay } = await verifySigned(req, env, db, text);
    if (!replay) await recordHeartbeat(db, parseJSON(text, {}));
    return json({ ok: true });
  }

  // ---- n8n workflow endpoints (token auth)
  if (path === "/api/n8n/jobs" && m === "POST") { await requireN8nToken(req, env); return json(await createJob(db, await body(req))); }
  if ((g = path.match(/^\/api\/n8n\/jobs\/([\w-]+)$/)) && m === "GET") {
    await requireN8nToken(req, env);
    const j = await db.prepare("SELECT * FROM jobs WHERE id = ?").bind(g[1]).first();
    if (!j) throw new HttpError(404, "Job not found");
    return json(jobView(j, true));
  }

  // ---- everything below needs an admin session
  if (!path.startsWith("/api/")) throw new HttpError(404, "Not found");
  await requireAdmin(req, env);

  if (path === "/api/stats" && m === "GET") return json(await stats(db, env));
  if (path === "/api/settings/app" && m === "GET") return json({ max_concurrency: await getMaxConcurrency(db, env), default_timezone: await getTimezone(db, env), n8n_windows: await getWindows(db) });
  if (path === "/api/settings/app" && m === "PUT") {
    const b = await body(req);
    if ("max_concurrency" in b) {
      const n = Number(b.max_concurrency);
      if (!Number.isInteger(n) || n < 1 || n > 100) throw new HttpError(400, "max_concurrency must be an integer between 1 and 100");
      await setApp(db, "max_concurrency", n);
    }
    if ("default_timezone" in b) { try { assertTimezone(String(b.default_timezone)); } catch (e) { throw new HttpError(400, e.message); } await setApp(db, "default_timezone", b.default_timezone); }
    if ("n8n_windows" in b) {
      if (!Array.isArray(b.n8n_windows)) throw new HttpError(400, "n8n_windows must be a list like [\"08:20-10:00\"]");
      try { b.n8n_windows.forEach(parseWindow); } catch (e) { throw new HttpError(400, e.message); }
      await setApp(db, "n8n_windows", JSON.stringify(b.n8n_windows));
    }
    if ("n8n_enabled" in b) await setApp(db, "n8n_enabled", b.n8n_enabled ? "1" : "0");
    return json({ max_concurrency: await getMaxConcurrency(db, env), default_timezone: await getTimezone(db, env), n8n_windows: await getWindows(db) });
  }
  if (path === "/api/n8n/status" && m === "GET") return json(await n8nStatus(db, env));

  // ---- jobs
  if (path === "/api/jobs" && m === "POST") return json(await createJob(db, await body(req)));
  if (path === "/api/jobs" && m === "GET") {
    const lim = Math.min(Number(url.searchParams.get("limit") || 100), 500), st = url.searchParams.get("status");
    const rows = st ? await db.prepare("SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?").bind(st, lim).all() : await db.prepare("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?").bind(lim).all();
    return json(rows.results.map((j) => jobView(j)));
  }
  if ((g = path.match(/^\/api\/jobs\/([\w-]+)$/)) && m === "GET") {
    const j = await db.prepare("SELECT * FROM jobs WHERE id = ?").bind(g[1]).first();
    if (!j) throw new HttpError(404, "Job not found");
    const logs = (await db.prepare("SELECT ts, level, message FROM job_logs WHERE job_id = ? ORDER BY id").bind(g[1]).all()).results;
    return json({ ...jobView(j, true), logs });
  }
  if ((g = path.match(/^\/api\/jobs\/([\w-]+)\/cancel$/)) && m === "POST") {
    const st = await cancel(db, g[1], { remoteCancel: async (id) => {
      const cfg = await githubConfig(db, env);
      const j = await db.prepare("SELECT remote_run_id FROM jobs WHERE id = ?").bind(id).first();
      let run = j.remote_run_id; if (!run) { const r = await findRun(cfg, runTitle(id)); run = r && String(r.id); }
      if (run) await cancelRun(cfg, run);
    } });
    if (!st) throw new HttpError(404, "Job not found");
    return json({ status: st });
  }
  if ((g = path.match(/^\/api\/jobs\/([\w-]+)\/retry$/)) && m === "POST") {
    const r = await retry(db, g[1]);
    if (r === "missing") throw new HttpError(404, "Job not found");
    if (r === "invalid") throw new HttpError(409, "Only failed or cancelled jobs can be retried");
    return json({ status: r });
  }
  if ((g = path.match(/^\/api\/jobs\/([\w-]+)\/remote-logs$/)) && m === "GET") {
    const cfg = await githubConfig(db, env);
    const j = await db.prepare("SELECT executor, remote_run_id FROM jobs WHERE id = ?").bind(g[1]).first();
    if (!j || j.executor !== "github") throw new HttpError(404, "No GitHub run for this job");
    let run = j.remote_run_id;
    if (!run) { const r = await findRun(cfg, runTitle(g[1])); if (!r) throw new HttpError(404, "Run not found yet"); run = String(r.id); await db.prepare("UPDATE jobs SET remote_run_id = ? WHERE id = ?").bind(run, g[1]).run(); }
    return json({ run_id: run, logs: await runLogsUrl(cfg, run) });
  }

  // ---- schedules
  if (path === "/api/schedules" && m === "GET") return json((await db.prepare("SELECT * FROM schedules ORDER BY created_at").all()).results.map(schedView));
  if (path === "/api/schedules/upcoming" && m === "GET") return json(await sched.upcoming(db, Math.min(Number(url.searchParams.get("limit") || 20), 100)));
  if (path === "/api/schedules" && m === "POST") return json(await saveSchedule(db, env, null, await body(req)));
  if ((g = path.match(/^\/api\/schedules\/([\w-]+)$/))) {
    if (m === "PUT") return json(await saveSchedule(db, env, g[1], await body(req)));
    if (m === "DELETE") {
      const r = await db.prepare("DELETE FROM schedules WHERE id = ?").bind(g[1]).run();
      if (!r.meta.changes) throw new HttpError(404, "Schedule not found");
      return json({ deleted: true });
    }
  }
  if ((g = path.match(/^\/api\/schedules\/([\w-]+)\/(pause|resume|run-now|runs)$/))) {
    const s = await db.prepare("SELECT * FROM schedules WHERE id = ?").bind(g[1]).first();
    if (!s) throw new HttpError(404, "Schedule not found");
    if (g[2] === "runs" && m === "GET") return json((await db.prepare("SELECT * FROM jobs WHERE schedule_id = ? ORDER BY created_at DESC LIMIT 50").bind(s.id).all()).results.map((j) => jobView(j)));
    if (m !== "POST") throw new HttpError(405, "Method not allowed");
    if (g[2] === "run-now") return json({ id: (await enqueue(db, { task: s.task, payload: JSON.parse(s.payload_json), priority: s.priority, scheduleId: s.id, workflow: s.workflow || s.name })).id });
    const enabled = g[2] === "resume";
    const next = enabled ? sched.computeNext(s) : null;
    await db.prepare("UPDATE schedules SET enabled = ?, next_run_at = ?, updated_at = ? WHERE id = ?").bind(enabled ? 1 : 0, next, now(), s.id).run();
    return json(schedView(await db.prepare("SELECT * FROM schedules WHERE id = ?").bind(s.id).first()));
  }
  throw new HttpError(404, "Not found");
}

function schedView(s) { return { id: s.id, name: s.name, task: s.task, workflow: s.workflow, payload: JSON.parse(s.payload_json), kind: s.kind, cron_expr: s.cron_expr, run_at: s.run_at,
  interval_seconds: s.interval_seconds, timezone: s.timezone, priority: s.priority, enabled: !!s.enabled, next_run_at: s.next_run_at, last_run_at: s.last_run_at }; }

async function createJob(db, b) {
  if (!USER_TASKS.includes(b.task)) throw new HttpError(400, `Unsupported task. Allowed: ${USER_TASKS.join(", ")}`);
  const payload = b.payload || {};
  if (JSON.stringify(payload).length > 64_000) throw new HttpError(413, "payload too large");
  const priority = b.priority ?? 5, maxAttempts = b.max_attempts ?? 3;
  if (!Number.isInteger(priority) || priority < 0 || priority > 10) throw new HttpError(400, "priority must be 0-10");
  if (!Number.isInteger(maxAttempts) || maxAttempts < 1 || maxAttempts > 10) throw new HttpError(400, "max_attempts must be 1-10");
  let runAfter = null;
  if (b.run_after) { runAfter = Date.parse(b.run_after); if (Number.isNaN(runAfter)) throw new HttpError(400, "run_after must be an ISO-8601 datetime with timezone"); }
  return enqueue(db, { task: b.task, payload, priority, maxAttempts, dedupeKey: b.dedupe_key || null, runAfter, workflow: b.workflow || "" });
}

async function saveSchedule(db, env, id, b) {
  if (b.task !== undefined && !USER_TASKS.includes(b.task)) throw new HttpError(400, "Unsupported task");
  const old = id ? await db.prepare("SELECT * FROM schedules WHERE id = ?").bind(id).first() : null;
  if (id && !old) throw new HttpError(404, "Schedule not found");
  const s = { name: b.name ?? old?.name, task: b.task ?? old?.task, workflow: b.workflow ?? old?.workflow ?? "", kind: b.kind ?? old?.kind ?? "cron",
    cron_expr: b.cron_expr ?? old?.cron_expr ?? "", interval_seconds: b.interval_seconds ?? old?.interval_seconds ?? 0, priority: b.priority ?? old?.priority ?? 5,
    timezone: b.timezone ?? old?.timezone ?? (await getTimezone(db, env)), payload_json: JSON.stringify(b.payload ?? (old ? JSON.parse(old.payload_json) : {})),
    run_at: old?.run_at ?? null, enabled: old ? old.enabled : (b.enabled === false ? 0 : 1) };
  if (!s.name || !s.task) throw new HttpError(400, "name and task are required");
  if (b.run_at) {
    const raw = String(b.run_at), hasZone = /(Z|[+-]\d\d:?\d\d)$/.test(raw);
    s.run_at = hasZone ? Date.parse(raw) : sched_localToUtc(s.timezone, raw);
    if (Number.isNaN(s.run_at)) throw new HttpError(400, "run_at must be an ISO-8601 datetime");
  }
  try { sched.validate(s); } catch (e) { throw new HttpError(400, e.message); }
  const t = now(), next = s.enabled ? sched.computeNext(s, t) : null;
  if (id) await db.prepare("UPDATE schedules SET name=?, task=?, workflow=?, payload_json=?, kind=?, cron_expr=?, run_at=?, interval_seconds=?, timezone=?, priority=?, next_run_at=?, updated_at=? WHERE id=?")
    .bind(s.name, s.task, s.workflow, s.payload_json, s.kind, s.cron_expr, s.run_at, s.interval_seconds, s.timezone, s.priority, next, t, id).run();
  else { id = sched.newId(); await db.prepare("INSERT INTO schedules (id, name, task, workflow, payload_json, kind, cron_expr, run_at, interval_seconds, timezone, priority, enabled, next_run_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    .bind(id, s.name, s.task, s.workflow, s.payload_json, s.kind, s.cron_expr, s.run_at, s.interval_seconds, s.timezone, s.priority, s.enabled, next, t, t).run(); }
  return schedView(await db.prepare("SELECT * FROM schedules WHERE id = ?").bind(id).first());
}
import { localToUtc } from "./cron.js";
function sched_localToUtc(tz, raw) {
  const m = /^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)/.exec(raw);
  return m ? localToUtc(tz, +m[1], +m[2], +m[3], +m[4], +m[5]) : NaN;
}

export async function tickAll(env, nowMs = now()) {
  const db = env.DB, out = {};
  out.schedules = await sched.tick(db, nowMs);
  out.n8n = await watchdog(env, db, { nowMs });
  out.jobs = await pump(env, db);
  await db.prepare("DELETE FROM job_logs WHERE ts < ?").bind(nowMs - 30 * 86400000).run();
  return out;
}

export default {
  async fetch(req, env, ctx) {
    try {
      const res = await route(req, env, ctx);
      if (req.method === "POST" && new URL(req.url).pathname === "/api/jobs") ctx?.waitUntil?.(pump(env, env.DB).catch(() => {}));   // start immediately, don't wait for the next tick
      res.headers.set("x-content-type-options", "nosniff");
      res.headers.set("x-frame-options", "DENY");
      res.headers.set("referrer-policy", "no-referrer");
      return res;
    } catch (e) {
      if (e instanceof HttpError) return json({ detail: e.message }, e.status, e.status === 429 ? { "retry-after": "60" } : {});
      console.error("unhandled", scrub(e && e.stack ? e.stack : String(e)));
      return json({ detail: "Internal error" }, 500);          // never echo internals
    }
  },
  async scheduled(_event, env, ctx) { ctx.waitUntil(tickAll(env)); },
};
