import { HTML } from "./ui-html.js";

export function dashboardResponse() {
  return new Response(HTML, { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store",
    "content-security-policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'" } });
}
