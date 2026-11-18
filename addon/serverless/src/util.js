export const now = () => Date.now();
export const uid = () => crypto.randomUUID();
export const json = (obj, status = 200, headers = {}) =>
  new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json", "cache-control": "no-store", ...headers } });
export class HttpError extends Error { constructor(status, message) { super(message); this.status = status; } }
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const parseJSON = (s, d = {}) => { try { return JSON.parse(s); } catch { return d; } };
