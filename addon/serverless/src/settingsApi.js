import { HttpError, json, now, parseJSON, uid } from "./util.js";
import { decryptJSON, encryptJSON, mask, maskMap } from "./crypto.js";
import { DEFAULT_ENDPOINTS, PROVIDERS, generate, testConnection } from "./llm.js";
import * as social from "./social.js";
import { resolveLLM, resolveSocial } from "./credentials.js";
import { githubConfig } from "./github.js";
import { UpstreamError, request } from "./http.js";

const urlOk = (v) => { if (v && !/^https?:\/\//.test(v)) throw new HttpError(400, "endpoint must start with http:// or https://"); return v || ""; };
const llmView = (r) => { const c = JSON.parse(r.config_json); return { id: r.id, name: r.name, provider: c.provider, model: c.model, endpoint: c.endpoint || "", api_key_masked: c.key_mask || "", is_default: !!r.is_default }; };
const socView = (r) => { const c = JSON.parse(r.config_json); return { id: r.id, platform: r.name, label: c.label || "", config: c.config || {}, secrets_masked: c.masked || {}, configured_fields: Object.keys(c.masked || {}).sort() }; };

export async function handleSettings(req, env, db, path, m, readBody) {
  let g;
  // ------------------------------------------------------------------ LLM
  if (path === "/api/settings/llm/providers" && m === "GET") return json({ providers: PROVIDERS, default_endpoints: DEFAULT_ENDPOINTS });
  if (path === "/api/settings/llm" && m === "GET") return json((await db.prepare("SELECT * FROM settings WHERE kind='llm' ORDER BY created_at").all()).results.map(llmView));
  if (path === "/api/settings/llm" && m === "POST") {
    const b = await readBody();
    const provider = String(b.provider || "").toLowerCase();
    if (!PROVIDERS.includes(provider)) throw new HttpError(422, `Unsupported provider. Use one of: ${PROVIDERS.join(", ")}`);
    if (!b.name || !b.model) throw new HttpError(422, "name and model are required");
    if (["openai-compatible", "custom", "local"].includes(provider) && !b.endpoint) throw new HttpError(400, "An endpoint is required for custom / OpenAI-compatible / local providers");
    const any = await db.prepare("SELECT COUNT(*) AS n FROM settings WHERE kind='llm'").first(), makeDefault = !!b.make_default || any.n === 0, id = uid(), t = now();
    if (makeDefault) await db.prepare("UPDATE settings SET is_default = 0 WHERE kind='llm'").run();
    await db.prepare("INSERT INTO settings (id, kind, name, config_json, secret_enc, is_default, created_at, updated_at) VALUES (?, 'llm', ?, ?, ?, ?, ?, ?)")
      .bind(id, b.name, JSON.stringify({ provider, model: b.model, endpoint: urlOk(b.endpoint), key_mask: mask(b.api_key) }), b.api_key ? await encryptJSON(env.ENCRYPTION_KEY, { api_key: b.api_key }) : "", makeDefault ? 1 : 0, t, t).run();
    return json(llmView(await db.prepare("SELECT * FROM settings WHERE id=?").bind(id).first()));
  }
  if ((g = path.match(/^\/api\/settings\/llm\/([\w-]+)(?:\/(default|test))?$/))) {
    const row = await db.prepare("SELECT * FROM settings WHERE id = ? AND kind='llm'").bind(g[1]).first();
    if (!row) throw new HttpError(404, "Provider not found");
    if (g[2] === "default" && m === "POST") { await db.prepare("UPDATE settings SET is_default = CASE WHEN id = ? THEN 1 ELSE 0 END WHERE kind='llm'").bind(row.id).run(); return json({ default: row.id }); }
    if (g[2] === "test" && m === "POST") return json(await testConnection(await resolveLLM(db, env, row.id)));
    if (!g[2] && m === "DELETE") { await db.prepare("DELETE FROM settings WHERE id=?").bind(row.id).run(); return json({ deleted: true }); }
    if (!g[2] && m === "PUT") {
      const b = await readBody(), c = JSON.parse(row.config_json); let secret = row.secret_enc;
      if (b.provider) { if (!PROVIDERS.includes(String(b.provider).toLowerCase())) throw new HttpError(400, "Unsupported provider"); c.provider = String(b.provider).toLowerCase(); }
      if (b.model) c.model = b.model;
      if (b.endpoint !== undefined && b.endpoint !== null) c.endpoint = urlOk(b.endpoint);
      if (b.api_key) { secret = await encryptJSON(env.ENCRYPTION_KEY, { api_key: b.api_key }); c.key_mask = mask(b.api_key); }   // blank = keep stored key
      await db.prepare("UPDATE settings SET name=?, config_json=?, secret_enc=?, updated_at=? WHERE id=?").bind(b.name || row.name, JSON.stringify(c), secret, now(), row.id).run();
      return json(llmView(await db.prepare("SELECT * FROM settings WHERE id=?").bind(row.id).first()));
    }
  }
  // --------------------------------------------------------------- social
  if (path === "/api/settings/social/platforms" && m === "GET") return json(social.spec());
  if (path === "/api/settings/social" && m === "GET") return json((await db.prepare("SELECT * FROM settings WHERE kind='social' ORDER BY created_at").all()).results.map(socView));
  if (path === "/api/settings/social" && m === "POST") {
    const b = await readBody(), p = String(b.platform || "").toLowerCase();
    if (!social.REGISTRY[p]) throw new HttpError(400, "Unsupported social platform");
    const secrets = b.secrets || {}, missing = social.missingFields(p, secrets);
    if (missing.length) throw new HttpError(400, `Missing required fields: ${missing.join(", ")}`);
    const id = uid(), t = now();
    await db.prepare("INSERT INTO settings (id, kind, name, config_json, secret_enc, created_at, updated_at) VALUES (?, 'social', ?, ?, ?, ?, ?)")
      .bind(id, p, JSON.stringify({ label: b.name || "", config: b.config || {}, masked: maskMap(secrets) }), await encryptJSON(env.ENCRYPTION_KEY, secrets), t, t).run();
    return json(socView(await db.prepare("SELECT * FROM settings WHERE id=?").bind(id).first()));
  }
  if ((g = path.match(/^\/api\/settings\/social\/([\w-]+)(?:\/(test))?$/))) {
    const row = await db.prepare("SELECT * FROM settings WHERE id = ? AND kind='social'").bind(g[1]).first();
    if (!row) throw new HttpError(404, "Account not found");
    if (g[2] === "test" && m === "POST") { const a = await resolveSocial(db, env, row.id); return json(await social.testAccount(a.platform, a.creds, a.cfg)); }
    if (!g[2] && m === "DELETE") { await db.prepare("DELETE FROM settings WHERE id=?").bind(row.id).run(); return json({ deleted: true }); }
    if (!g[2] && m === "PUT") {
      const b = await readBody(), merged = await decryptJSON(env.ENCRYPTION_KEY, row.secret_enc);
      for (const [k, v] of Object.entries(b.secrets || {})) if (v) merged[k] = v;               // blank = keep stored value
      const missing = social.missingFields(row.name, merged);
      if (missing.length) throw new HttpError(400, `Missing required fields: ${missing.join(", ")}`);
      await db.prepare("UPDATE settings SET config_json=?, secret_enc=?, updated_at=? WHERE id=?")
        .bind(JSON.stringify({ label: b.name ?? "", config: b.config || {}, masked: maskMap(merged) }), await encryptJSON(env.ENCRYPTION_KEY, merged), now(), row.id).run();
      return json(socView(await db.prepare("SELECT * FROM settings WHERE id=?").bind(row.id).first()));
    }
  }
  // --------------------------------------------------------------- github
  if (path === "/api/settings/github" && m === "GET") {
    const row = await db.prepare("SELECT * FROM settings WHERE kind='github' LIMIT 1").first();
    if (!row) return json({ configured: false });
    return json({ configured: true, ...JSON.parse(row.config_json), token_masked: mask((await decryptJSON(env.ENCRYPTION_KEY, row.secret_enc)).token) });
  }
  if (path === "/api/settings/github" && m === "PUT") {
    const b = await readBody(), cfg = Object.fromEntries(["owner", "repo", "workflow", "n8n_workflow", "ref"].map((k) => [k, String(b[k] ?? "").trim()]));
    const row = await db.prepare("SELECT * FROM settings WHERE kind='github' LIMIT 1").first(), t = now();
    const old = row ? await decryptJSON(env.ENCRYPTION_KEY, row.secret_enc) : {};
    const enc = await encryptJSON(env.ENCRYPTION_KEY, { token: b.token || old.token || "" });
    if (row) await db.prepare("UPDATE settings SET config_json=?, secret_enc=?, updated_at=? WHERE id=?").bind(JSON.stringify(cfg), enc, t, row.id).run();
    else await db.prepare("INSERT INTO settings (id, kind, name, config_json, secret_enc, created_at, updated_at) VALUES (?, 'github', 'github', ?, ?, ?, ?)").bind(uid(), JSON.stringify(cfg), enc, t, t).run();
    return json({ configured: true, ...cfg, token_masked: mask(b.token || old.token) });
  }
  if (path === "/api/settings/github/test" && m === "POST") {
    const cfg = await githubConfig(db, env);
    if (!cfg.token) return json({ ok: false, message: "GitHub is not configured" });
    try {
      const r = await request("GET", `https://api.github.com/repos/${cfg.owner}/${cfg.repo}/actions/workflows/${cfg.workflow}`, { headers: { authorization: `Bearer ${cfg.token}`, accept: "application/vnd.github+json", "user-agent": "suite-worker" }, secrets: [cfg.token] });
      const d = await r.json(); return json({ ok: true, workflow: d.name, state: d.state });
    } catch (e) { return json({ ok: false, message: e instanceof UpstreamError ? e.message.slice(0, 400) : "Unexpected error" }); }
  }
  // ------------------------------------------------- n8n helper endpoints
  return null;
}

export async function n8nGenerate(db, env, b) {
  const prompt = String(b.prompt || "").trim();
  if (!prompt) throw new HttpError(400, "prompt is required");
  let cfg;
  try { cfg = await resolveLLM(db, env, b.provider_id || null, b.model || null); } catch (e) { throw new HttpError(404, e.message); }
  try { return { text: await generate(cfg, prompt, { system: String(b.system || ""), maxTokens: Number(b.max_tokens || 800) }), provider: cfg.provider, model: cfg.model }; }
  catch (e) { throw new HttpError(502, String(e.message).slice(0, 300)); }
}
export async function n8nAccounts(db) {
  return { social: (await db.prepare("SELECT id, name, config_json FROM settings WHERE kind='social'").all()).results.map((x) => ({ id: x.id, platform: x.name, label: parseJSON(x.config_json).label || "" })),
    llm: (await db.prepare("SELECT id, name, is_default FROM settings WHERE kind='llm'").all()).results.map((x) => ({ id: x.id, name: x.name, default: !!x.is_default })) };
}
