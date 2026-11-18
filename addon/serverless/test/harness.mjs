import { Miniflare } from "miniflare";
import { build } from "esbuild";
import { readFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { signMessage } from "../src/crypto.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
let bundle;
async function code() {
  if (!bundle) bundle = (await build({ entryPoints: [join(ROOT, "src/index.js")], bundle: true, format: "esm", write: false, platform: "neutral", target: "es2022" })).outputFiles[0].text;
  return bundle;
}
export const KEY = Buffer.alloc(32, 7).toString("base64");
export const SECRETS = { ADMIN_USERNAME: "admin", ADMIN_PASSWORD: "correct horse battery", SESSION_SECRET: "s".repeat(48), ENCRYPTION_KEY: KEY,
  CALLBACK_SECRET: "cb-secret-for-tests-0123456789", N8N_ADDON_API_TOKEN: "n8n-token-for-tests-0123456789", DEFAULT_TIMEZONE: "Africa/Lagos", MAX_CONCURRENT_JOBS: "5", MAX_JOBS_PER_TICK: "25" };

export function migrationStatements() {
  const sql = readFileSync(join(ROOT, "migrations/0001_init.sql"), "utf8").split("\n").map((l) => l.replace(/--.*$/, "")).join("\n");
  return sql.split(/;\s*\n/).map((s) => s.replace(/\s+/g, " ").trim()).filter(Boolean);
}

export async function makeWorker({ persist, vars = {}, outbound } = {}) {
  persist = persist || mkdtempSync(join(tmpdir(), "d1-"));
  const calls = [];
  const mf = new Miniflare({ modules: true, script: await code(), compatibilityDate: "2025-01-01", d1Databases: ["DB"], d1Persist: persist,
    bindings: { ...SECRETS, ...vars }, outboundService: async (req) => { calls.push({ url: req.url, method: req.method, headers: Object.fromEntries(req.headers), body: await req.text() }); return outbound ? outbound(req, calls) : req.url.endsWith("/dispatches") ? new Response(null, { status: 204 }) : new Response("{}", { status: 200 }); } });
  const db = await mf.getD1Database("DB");
  await db.batch(migrationStatements().map((s) => db.prepare(s)));
  const worker = await mf.getWorker();
  const h = { mf, db, calls, persist, worker, csrf: null, cookie: null,
    async req(path, { method = "GET", body, headers = {}, raw } = {}) {
      const hd = { ...headers };
      if (h.cookie) hd.cookie = h.cookie;
      if (h.csrf && method !== "GET") hd["x-csrf-token"] = h.csrf;
      const res = await mf.dispatchFetch("http://suite.test" + path, { method, headers: { ...(body !== undefined && raw === undefined ? { "content-type": "application/json" } : {}), ...hd }, body: raw !== undefined ? raw : body !== undefined ? JSON.stringify(body) : undefined });
      const text = await res.text();
      let data; try { data = JSON.parse(text); } catch { data = text; }
      return { status: res.status, data, headers: res.headers };
    },
    async login() {
      const r = await h.req("/api/auth/login", { method: "POST", body: { username: "admin", password: SECRETS.ADMIN_PASSWORD } });
      if (r.status !== 200) throw new Error("login failed " + JSON.stringify(r.data));
      h.cookie = r.headers.get("set-cookie").split(";")[0]; h.csrf = r.data.csrf; return h;
    },
    tick: () => worker.scheduled({ cron: "* * * * *" }),
    async signed(path, bodyText, { method = "POST", secret = SECRETS.CALLBACK_SECRET, ts, nonce } = {}) {
      ts = ts ?? String(Math.floor(Date.now() / 1000)); nonce = nonce ?? crypto.randomUUID().replaceAll("-", "");
      const sig = await signMessage(secret, ts, nonce, bodyText);
      return h.req(path, { method, raw: method === "GET" ? undefined : bodyText, headers: { "x-suite-timestamp": ts, "x-suite-nonce": nonce, "x-suite-signature": sig, "content-type": "application/json" } });
    },
    async q(sql, ...a) { return (await db.prepare(sql).bind(...a).all()).results; },
    async count(status) { return (await db.prepare("SELECT COUNT(*) AS n FROM jobs WHERE status = ?").bind(status).first()).n; },
    async dispose() { await mf.dispose(); },
  };
  return h;
}
export const waitFor = async (fn, ms = 20000, step = 50) => { const end = Date.now() + ms; while (Date.now() < end) { if (await fn()) return true; await new Promise((r) => setTimeout(r, step)); } return false; };
