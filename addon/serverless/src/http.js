// Single outbound HTTP entry point: classifies failures and scrubs secrets from messages.
import { scrub } from "./crypto.js";

export class UpstreamError extends Error {
  constructor(message, retryable = false, retryAfter = 0) { super(message); this.name = "UpstreamError"; this.retryable = retryable; this.retryAfter = retryAfter; }
}
export const Permanent = (m) => new UpstreamError(m, false);

export function retryAfterSeconds(res) {
  const v = res.headers.get("retry-after");
  if (v && /^\d+(\.\d+)?$/.test(v)) return Math.max(0, Number(v));
  const reset = res.headers.get("x-rate-limit-reset");                     // X: epoch seconds
  if (reset && /^\d+$/.test(reset)) return Math.max(0, Number(reset) - Math.floor(Date.now() / 1000));
  return 0;
}

/** fetch wrapper: network errors, 429 and 5xx -> retryable; other 4xx -> permanent. */
export async function request(method, url, { headers = {}, body, secrets = [], fetchFn = fetch, raw = false } = {}) {
  let res;
  try { res = await fetchFn(url, { method, headers, body }); }
  catch (e) { throw new UpstreamError(`Network error: ${scrub(e.message, secrets).slice(0, 300)}`, true); }
  if (res.status < 400) return res;
  const text = scrub((await res.text()).slice(0, 400), secrets);
  if (res.status === 429 || res.status >= 500) throw new UpstreamError(`HTTP ${res.status}: ${text}`, true, retryAfterSeconds(res));
  throw new UpstreamError(`HTTP ${res.status}${res.status === 401 || res.status === 403 ? " (check token validity/permissions)" : ""}: ${text}`, false);
}
export const jsonBody = (o) => JSON.stringify(o);
export const form = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== undefined && v !== null).map(([k, v]) => [k, String(v)]));
