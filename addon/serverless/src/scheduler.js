import { assertTimezone, nextCron } from "./cron.js";
import { enqueue } from "./queue.js";
import { now, uid } from "./util.js";

export const MISFIRE_GRACE_MS = 3600 * 1000;

export function computeNext(s, afterMs = now()) {
  if (s.kind === "once") return s.run_at && s.run_at > afterMs ? s.run_at : null;
  if (s.kind === "interval") return afterMs + s.interval_seconds * 1000;
  if (s.kind === "cron") return nextCron(s.cron_expr, s.timezone, afterMs);
  throw new Error("kind must be cron, once or interval");
}

export function validate(s) {
  assertTimezone(s.timezone);
  if (s.kind === "cron") nextCron(s.cron_expr, s.timezone, now());
  else if (s.kind === "once") { if (!s.run_at) throw new Error("run_at is required for one-time schedules"); }
  else if (s.kind === "interval") { if (!(s.interval_seconds >= 30)) throw new Error("interval_seconds must be at least 30"); }
  else throw new Error("kind must be cron, once or interval");
}

export async function tick(db, nowMs = now(), grace = MISFIRE_GRACE_MS) {
  const { results } = await db.prepare("SELECT * FROM schedules WHERE enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?").bind(nowMs).all();
  let created = 0;
  for (const s of results) {
    const fire = s.next_run_at, key = `sched:${s.id}:${fire}`;
    const skip = s.kind !== "once" && nowMs - fire > grace;
    const already = await db.prepare("SELECT 1 AS x FROM jobs WHERE dedupe_key = ?").bind(key).first();
    if (!skip && !already) {
      const r = await enqueue(db, { task: s.task, payload: JSON.parse(s.payload_json), priority: s.priority, dedupeKey: key, scheduleId: s.id, workflow: s.workflow || s.name });
      if (r.created) created++;
    }
    const next = s.kind === "once" ? null : computeNext(s, Math.max(nowMs, fire));
    await db.prepare("UPDATE schedules SET next_run_at = ?, enabled = ?, last_run_at = ?, updated_at = ? WHERE id = ?")
      .bind(next, s.kind === "once" ? 0 : 1, skip ? s.last_run_at : nowMs, nowMs, s.id).run();
  }
  return created;
}

export async function upcoming(db, limit = 20, per = 3) {
  const { results } = await db.prepare("SELECT * FROM schedules WHERE enabled = 1 AND next_run_at IS NOT NULL").all();
  const out = [];
  for (const s of results) {
    let at = s.next_run_at;
    for (let i = 0; i < per && at; i++) {
      out.push({ schedule_id: s.id, name: s.name, task: s.task, at_utc: at, timezone: s.timezone });
      at = s.kind === "once" ? null : computeNext(s, at);
    }
  }
  return out.sort((a, b) => a.at_utc - b.at_utc).slice(0, limit);
}
export const newId = uid;
