export function dashboardResponse() {
  return new Response("<!doctype html><title>Automation Suite</title><p>Dashboard is added in stage 4.</p>", { headers: { "content-type": "text/html; charset=utf-8", "content-security-policy": "default-src 'self'" } });
}
