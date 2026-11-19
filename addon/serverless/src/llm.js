// LLM providers: DeepSeek, OpenAI, Gemini, Anthropic, OpenAI-compatible, custom and local endpoints.
import { Permanent, UpstreamError, jsonBody, request } from "./http.js";

const OPENAI_STYLE = new Set(["openai", "deepseek", "openai-compatible", "custom", "local"]);
export const PROVIDERS = [...OPENAI_STYLE, "gemini", "anthropic"].sort();
export const DEFAULT_ENDPOINTS = { openai: "https://api.openai.com/v1", deepseek: "https://api.deepseek.com/v1",
  gemini: "https://generativelanguage.googleapis.com/v1beta", anthropic: "https://api.anthropic.com/v1" };

export function buildRequest({ provider, model, endpoint, key, prompt, system = "", maxTokens = 1024 }) {
  const p = provider.toLowerCase(), base = (endpoint || DEFAULT_ENDPOINTS[p] || "").replace(/\/+$/, "");
  if (!base) throw Permanent("An endpoint is required for this provider");
  if (OPENAI_STYLE.has(p)) {
    const headers = { "content-type": "application/json", ...(key ? { authorization: `Bearer ${key}` } : {}) };   // local models often need no key
    return { url: `${base}/chat/completions`, headers, body: { model, max_tokens: maxTokens, messages: [...(system ? [{ role: "system", content: system }] : []), { role: "user", content: prompt }] } };
  }
  if (p === "gemini") {                                                       // key goes in a header, never the URL
    return { url: `${base}/models/${model}:generateContent`, headers: { "x-goog-api-key": key, "content-type": "application/json" },
      body: { contents: [{ role: "user", parts: [{ text: prompt }] }], generationConfig: { maxOutputTokens: maxTokens }, ...(system ? { systemInstruction: { parts: [{ text: system }] } } : {}) } };
  }
  if (p === "anthropic") {
    return { url: `${base}/messages`, headers: { "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json" },
      body: { model, max_tokens: maxTokens, messages: [{ role: "user", content: prompt }], ...(system ? { system } : {}) } };
  }
  throw Permanent(`Unsupported provider: ${provider}`);
}

export function parseResponse(provider, data) {
  const p = provider.toLowerCase();
  try {
    if (OPENAI_STYLE.has(p)) return data.choices[0].message.content ?? "";
    if (p === "gemini") return data.candidates[0].content.parts.map((x) => x.text || "").join("");
    if (p === "anthropic") return data.content.filter((b) => b.type === "text").map((b) => b.text).join("");
  } catch { throw Permanent(`Unexpected ${provider} response shape`); }
  if (OPENAI_STYLE.has(p) || p === "gemini" || p === "anthropic") throw Permanent(`Unexpected ${provider} response shape`);
  throw Permanent(`Unsupported provider: ${provider}`);
}

export async function generate(cfg, prompt, { system = "", maxTokens = 1024, fetchFn = fetch } = {}) {
  const r = buildRequest({ ...cfg, prompt, system, maxTokens });
  const res = await request("POST", r.url, { headers: r.headers, body: jsonBody(r.body), secrets: [cfg.key], fetchFn });
  return parseResponse(cfg.provider, await res.json());
}

/** Tiny real request. Never throws; returns {ok, message}. */
export async function testConnection(cfg, fetchFn = fetch) {
  try { const text = await generate(cfg, "Reply with the single word: OK", { maxTokens: 16, fetchFn }); return { ok: true, message: "Connection successful", model: cfg.model, sample: text.slice(0, 40) }; }
  catch (e) { return { ok: false, message: e instanceof UpstreamError ? e.message.slice(0, 400) : `${e.name}: ${String(e.message).slice(0, 300)}` }; }
}
