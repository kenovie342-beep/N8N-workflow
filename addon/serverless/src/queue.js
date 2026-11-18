// Persistent job queue on D1. Concurrency is enforced INSIDE one atomic SQL statement, so overlapping
// Worker invocations can never claim more than (max_concurrency - running) jobs.
import { getMaxConcurrency } from "./settings.js";
import { scrub } from "./crypto.js";
import { now, uid } from "./util.js";

export const FINAL = ["completed", "failed", "cancelled"];
export const LEASE_MS = 5 * 60 * 1000;
export const backoffMs = (attempts, retryAfterSec = 0) => Math.max(Math.min(5000 * 2 ** Math.max(0, attempts - 1), 900000), retryAfterSec * 1000);

export async function addLog(db, jobId, message, level = "info") {
  await db.prepare("INSERT INTO job_logs (job_id, ts, level, message) VALUES (?, ?, ?, ?)").bind(jobId, now(), level, scrub(message).slice(0, 4000)).run();
}

export async function enqueue(db, { task, payload = {}, priority = 5, maxAttempts = 3, dedupeKey = null, runAfter = null, scheduleId = null, workflow = "" }) {
  const id = uid(), t = now();
  const executor = task === "github_job" ? "github" : "worker";
  const r = await db.prepare(
    `INSERT INTO jobs (id, task, workflow, payload_json, status, priority, max_attempts, dedupe_key, schedule_id, executor, run_after, created_at, updated_at)
     VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING`)
    .bind(id, task, workflow, JSON.stringify(payload), priority, maxAttempts, dedupeKey, scheduleId, executor, runAfter ?? t, t, t).run();
  if (r.meta.changes === 0) {                                  // active duplicate: return the existing job
    const ex = await db.prepare("SELECT id, status FROM jobs WHERE dedupe_key = ? AND status IN ('queued','running')").bind(dedupeKey).first();
    return { id: ex.id, status: ex.status, created: false };
  }
  return { id, status: "queued", created: true };
}

/** Atomically move up to min(perTick, max - running) queued jobs to running and return them. */
export async function claim(db, env, { perTick = 10, nowMs = now() } = {}) {
  const max = await getMaxConcurrency(db, env);
  const { results } = await db.prepare(
    `UPDATE jobs SET status = 'running', attempts = attempts + 1, started_at = ?1, updated_at = ?1, lease_expires_at = ?2, cancel_requested = 0
     WHERE id IN (
       SELECT id FROM jobs WHERE status = 'queued' AND run_after <= ?1
       ORDER BY priority DESC, created_at ASC
       LIMIT MIN(?3, MAX(0, ?4 - (SELECT COUNT(*) FROM jobs WHERE status = 'running'))))
     RETURNING id, task, payload_json, attempts, max_attempts, executor`)
    .bind(nowMs, nowMs + LEASE_MS, perTick, max).all();
  return results;
}

export async function complete(db, id, result = {}) {
  const t = now();
  const r = await db.prepare("UPDATE jobs SET status='completed', result_json=?, error='', finished_at=?, updated_at=?, lease_expires_at=NULL WHERE id=? AND status='running'")
    .bind(JSON.stringify(result), t, t, id).run();
  return r.meta.changes > 0;
}

/** running -> queued (retry with backoff) or failed. Returns new status or null if the job was not running. */
export async function fail(db, id, error, { retryable = true, retryAfter = 0 } = {}) {
  const job = await db.prepare("SELECT attempts, max_attempts, status FROM jobs WHERE id = ?").bind(id).first();
  if (!job || job.status !== "running") return null;
  const t = now(), msg = scrub(error).slice(0, 1500);
  const willRetry = retryable && job.attempts < job.max_attempts;
  const r = willRetry
    ? await db.prepare("UPDATE jobs SET status='queued', error=?, run_after=?, updated_at=?, lease_expires_at=NULL WHERE id=? AND status='running'").bind(msg, t + backoffMs(job.attempts, retryAfter), t, id).run()
    : await db.prepare("UPDATE jobs SET status='failed', error=?, finished_at=?, updated_at=?, lease_expires_at=NULL WHERE id=? AND status='running'").bind(msg, t, t, id).run();
  if (r.meta.changes === 0) return null;
  await addLog(db, id, `${willRetry ? "retrying" : "failed"}: ${msg}`, "error");
  return willRetry ? "queued" : "failed";
}

export async function cancel(db, id, { remoteCancel } = {}) {
  const job = await db.prepare("SELECT status, executor FROM jobs WHERE id = ?").bind(id).first();
  if (!job) return null;
  if (FINAL.includes(job.status)) return job.status;
  const t = now();
  if (job.status === "queued") {
    await db.prepare("UPDATE jobs SET status='cancelled', finished_at=?, updated_at=? WHERE id=? AND status='queued'").bind(t, t, id).run();
    return "cancelled";
  }
  if (job.executor === "github" && remoteCancel) {
    try { await remoteCancel(id); } catch (e) {
      await db.prepare("UPDATE jobs SET error=?, cancel_requested=1 WHERE id=?").bind(`Remote cancel failed: ${scrub(e.message)}`.slice(0, 500), id).run();
      return "running";
    }
    await db.prepare("UPDATE jobs SET status='cancelled', finished_at=?, updated_at=?, lease_expires_at=NULL WHERE id=? AND status='running'").bind(t, t, id).run();
    return "cancelled";
  }
  await db.prepare("UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=?").bind(t, id).run();
  return "running";
}

export async function retry(db, id) {
  const job = await db.prepare("SELECT status FROM jobs WHERE id = ?").bind(id).first();
  if (!job) return "missing";
  if (!["failed", "cancelled"].includes(job.status)) return "invalid";
  const t = now();
  await db.prepare("UPDATE jobs SET status='queued', attempts=0, error='', finished_at=NULL, run_after=?, updated_at=?, cancel_requested=0 WHERE id=?").bind(t, t, id).run();
  return "queued";
}

/** Interrupted executions (isolate died, lost callback): lease expired -> retry or fail. */
export async function recoverExpired(db, nowMs = now()) {
  const { results } = await db.prepare("SELECT id FROM jobs WHERE status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?").bind(nowMs).all();
  let n = 0;
  for (const r of results) if (await fail(db, r.id, "Interrupted: execution lease expired (worker stopped or callback lost)")) n++;
  return n;
}

export async function stats(db, env) {
  const { results } = await db.prepare("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").all();
  const c = Object.fromEntries(results.map((r) => [r.status, r.n]));
  const avg = await db.prepare("SELECT AVG(finished_at - started_at) AS a FROM (SELECT started_at, finished_at FROM jobs WHERE status='completed' AND started_at IS NOT NULL ORDER BY finished_at DESC LIMIT 200)").first();
  const recent = await db.prepare("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT 10").all();
  return { total: Object.values(c).reduce((a, b) => a + b, 0), queued: c.queued || 0, running: c.running || 0, completed: c.completed || 0,
    failed: c.failed || 0, cancelled: c.cancelled || 0, max_concurrency: await getMaxConcurrency(db, env),
    avg_duration_seconds: avg?.a != null ? Math.round(avg.a) / 1000 : null, recent: recent.results.map(jobView) };
}

export function jobView(j, full = false) {
  const d = { id: j.id, task: j.task, workflow: j.workflow, status: j.status, priority: j.priority, attempts: j.attempts, max_attempts: j.max_attempts,
    error: j.error, executor: j.executor, schedule_id: j.schedule_id, created_at: j.created_at, started_at: j.started_at, finished_at: j.finished_at,
    run_after: j.run_after, duration_seconds: j.finished_at && j.started_at ? (j.finished_at - j.started_at) / 1000 : null };
  if (full) {
    d.result = JSON.parse(j.result_json || "{}");
    d.payload = Object.fromEntries(Object.entries(JSON.parse(j.payload_json || "{}")).map(([k, v]) => [k, /key|secret|token|password/i.test(k) ? "***" : v]));
    d.remote_run_id = j.remote_run_id;
  }
  return d;
}
