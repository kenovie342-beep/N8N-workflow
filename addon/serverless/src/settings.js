import { parseJSON } from "./util.js";

export async function getApp(db, key, dflt = "") {
  const r = await db.prepare("SELECT value FROM app_settings WHERE key = ?").bind(key).first();
  return r ? r.value : dflt;
}
export async function setApp(db, key, value) {
  await db.prepare("INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value").bind(key, String(value)).run();
}
export async function getMaxConcurrency(db, env) {
  const v = await getApp(db, "max_concurrency", "");
  const n = /^\d+$/.test(v) ? Number(v) : Number(env.MAX_CONCURRENT_JOBS || 5);
  return Math.max(1, Math.min(n, 100));
}
export async function getTimezone(db, env) { return (await getApp(db, "default_timezone", "")) || env.DEFAULT_TIMEZONE || "Africa/Lagos"; }
export const DEFAULT_WINDOWS = ["08:20-10:00", "13:20-15:00", "18:20-20:00"];
export async function getWindows(db) { return parseJSON(await getApp(db, "n8n_windows", ""), DEFAULT_WINDOWS); }
export async function n8nEnabled(db) { return (await getApp(db, "n8n_enabled", "1")) === "1"; }
