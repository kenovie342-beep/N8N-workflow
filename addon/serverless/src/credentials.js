// Server-side credential retrieval. Decryption happens only here, at the point of use.
import { decryptJSON } from "./crypto.js";

export async function resolveLLM(db, env, providerId = null, modelOverride = null) {
  const row = providerId
    ? await db.prepare("SELECT * FROM settings WHERE id = ? AND kind = 'llm'").bind(providerId).first()
    : (await db.prepare("SELECT * FROM settings WHERE kind = 'llm' AND is_default = 1 LIMIT 1").first()) || (await db.prepare("SELECT * FROM settings WHERE kind = 'llm' LIMIT 1").first());
  if (!row) throw Object.assign(new Error("LLM provider not found (add one in Settings -> AI Providers)"), { permanent: true });
  const cfg = JSON.parse(row.config_json), sec = await decryptJSON(env.ENCRYPTION_KEY, row.secret_enc);
  return { id: row.id, provider: cfg.provider, model: modelOverride || cfg.model, endpoint: cfg.endpoint || "", key: sec.api_key || "" };
}
export async function resolveSocial(db, env, accountId) {
  const row = await db.prepare("SELECT * FROM settings WHERE id = ? AND kind = 'social'").bind(accountId).first();
  if (!row) throw Object.assign(new Error("Social account not found"), { permanent: true });
  return { platform: row.name, creds: await decryptJSON(env.ENCRYPTION_KEY, row.secret_enc), cfg: JSON.parse(row.config_json).config || {} };
}
