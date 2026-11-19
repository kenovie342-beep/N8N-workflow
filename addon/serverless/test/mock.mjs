// Tiny fetch router for unit tests. Records every call; unmatched requests fail loudly.
export function mockFetch(routes = []) {
  const calls = [];
  const fn = async (url, init = {}) => {
    const u = String(url), method = (init.method || "GET").toUpperCase();
    let body = init.body;
    if (body instanceof URLSearchParams) body = body.toString();
    else if (body instanceof FormData) body = Object.fromEntries([...body.entries()].map(([k, v]) => [k, typeof v === "string" ? v : `[file:${v.size}]`]));
    else if (body instanceof ArrayBuffer) body = `[bytes:${body.byteLength}]`;
    calls.push({ url: u, method, headers: Object.fromEntries(Object.entries(init.headers || {}).map(([k, v]) => [k.toLowerCase(), v])), body });
    const r = routes.find((x) => (!x.method || x.method === method) && (x.match instanceof RegExp ? x.match.test(u) : u.startsWith(x.match)) && (x.times === undefined || x.times > 0));
    if (!r) throw new Error(`unmocked request: ${method} ${u}`);
    if (r.times !== undefined) r.times--;
    const res = typeof r.respond === "function" ? r.respond({ url: u, method, body }) : r.respond;
    return res instanceof Response ? res : Response.json(res ?? {}, { status: r.status || 200, headers: r.headers });
  };
  fn.calls = calls;
  return fn;
}
export const parseBody = (b) => { try { return JSON.parse(b); } catch { return Object.fromEntries(new URLSearchParams(String(b))); } };
export const deps = (routes) => { const f = mockFetch(routes); return { fetchFn: f, sleep: async () => {}, calls: f.calls }; };
