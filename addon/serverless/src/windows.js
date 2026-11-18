// n8n host windows: the Worker starts an n8n run inside GitHub Actions before each window and watches its heartbeat.
import { localParts, localToUtc } from "./cron.js";
import { dispatchN8n, githubConfig } from "./github.js";
import { getTimezone, getWindows, n8nEnabled } from "./settings.js";
import { now } from "./util.js";

export const LEAD_MS = 10 * 60 * 1000;        // start n8n this long before a window opens
export const BEAT_STALE_MS = 3 * 60 * 1000;   // heartbeat older than this = n8n is not up
export const STARTUP_GRACE_MS = 8 * 60 * 1000; // do not re-dispatch while a start is still booting

export function parseWindow(w) {
  const m = /^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$/.exec(String(w).trim());
  if (!m) throw new Error(`Invalid window "${w}" (use HH:MM-HH:MM)`);
  const [a, b, c, d] = m.slice(1).map(Number);
  if (a > 23 || c > 23 || b > 59 || d > 59 || a * 60 + b >= c * 60 + d) throw new Error(`Invalid window "${w}" (end must be after start, same day)`);
  return { sh: a, sm: b, eh: c, em: d };
}

/** Returns the window that should be active at nowMs (including the lead time), or null. */
export function activeWindow(windows, tz, nowMs) {
  const p = localParts(tz, nowMs);
  for (const w of windows) {
    const { sh, sm, eh, em } = parseWindow(w);
    const start = localToUtc(tz, p.y, p.mo, p.d, sh, sm), end = localToUtc(tz, p.y, p.mo, p.d, eh, em);
    if (nowMs >= start - LEAD_MS && nowMs < end) return { start, end, label: w };
  }
  return null;
}

/** Pure decision function (unit-tested). */
export function decide({ win, lastBeat, lastDispatch, nowMs }) {
  if (!win) return "none";
  const beatFresh = lastBeat && nowMs - lastBeat < BEAT_STALE_MS;
  if (beatFresh) return "none";
  if (lastDispatch && nowMs - lastDispatch < STARTUP_GRACE_MS) return "wait";
  return "start";
}

export async function watchdog(env, db, { nowMs = now(), dispatch = dispatchN8n } = {}) {
  if (!(await n8nEnabled(db))) return { action: "disabled" };
  const tz = await getTimezone(db, env);
  const win = activeWindow(await getWindows(db), tz, nowMs);
  const st = await db.prepare("SELECT last_beat, last_dispatch FROM n8n_state WHERE id = 1").first();
  const action = decide({ win, lastBeat: st.last_beat, lastDispatch: st.last_dispatch, nowMs });
  if (action !== "start") return { action, window: win?.label };
  try {
    await dispatch(await githubConfig(db, env), new Date(win.end).toISOString());
    await db.prepare("UPDATE n8n_state SET last_dispatch = ?, window_end = ? WHERE id = 1").bind(nowMs, win.end).run();
    return { action: "started", window: win.label };
  } catch (e) {
    return { action: "dispatch_failed", error: e.message };   // retried on the next tick (every minute)
  }
}

export async function recordHeartbeat(db, { run_id, tunnel_url }, nowMs = now()) {
  await db.prepare("UPDATE n8n_state SET last_beat = ?, run_id = ?, tunnel_url = ? WHERE id = 1").bind(nowMs, String(run_id || ""), String(tunnel_url || "")).run();
}
export async function n8nStatus(db, env, nowMs = now()) {
  const st = await db.prepare("SELECT * FROM n8n_state WHERE id = 1").first();
  const tz = await getTimezone(db, env);
  const win = activeWindow(await getWindows(db), tz, nowMs);
  return { enabled: await n8nEnabled(db), windows: await getWindows(db), timezone: tz, in_window: !!win, window_label: win?.label || null,
    up: !!(st.last_beat && nowMs - st.last_beat < BEAT_STALE_MS), last_beat: st.last_beat, run_id: st.run_id, last_dispatch: st.last_dispatch };
}
