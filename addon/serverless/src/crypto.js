const enc = new TextEncoder();
const dec = new TextDecoder();
export const b64 = (u8) => btoa(String.fromCharCode(...u8));
export const unb64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
export const b64url = (u8) => b64(u8).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
export const unb64url = (s) => unb64(s.replaceAll("-", "+").replaceAll("_", "/") + "=".repeat((4 - (s.length % 4)) % 4));
const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");

export async function hmacHex(secret, message) {
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return hex(await crypto.subtle.sign("HMAC", key, enc.encode(message)));
}
export function safeEqual(a, b) {
  a = String(a); b = String(b);
  let r = a.length ^ b.length;
  for (let i = 0; i < Math.max(a.length, b.length); i++) r |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  return r === 0;
}
/** Compare two secrets without leaking length/timing. */
export async function secretsEqual(a, b, salt = "cmp") {
  return safeEqual(await hmacHex(salt, String(a)), await hmacHex(salt, String(b)));
}
export const signMessage = (secret, ts, nonce, body) => hmacHex(secret, `${ts}.${nonce}.${body}`);

// ---- AES-GCM encryption of stored secrets (key = base64 of 32 random bytes) ----
async function aesKey(keyB64) {
  const raw = unb64(keyB64);
  if (raw.length !== 32) throw new Error("ENCRYPTION_KEY must be 32 bytes, base64 encoded");
  return crypto.subtle.importKey("raw", raw, "AES-GCM", false, ["encrypt", "decrypt"]);
}
export async function encryptJSON(keyB64, obj) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, await aesKey(keyB64), enc.encode(JSON.stringify(obj)));
  return `v1.${b64(iv)}.${b64(new Uint8Array(ct))}`;
}
export async function decryptJSON(keyB64, blob) {
  if (!blob) return {};
  const [v, iv, ct] = blob.split(".");
  if (v !== "v1") throw new Error("Unknown ciphertext version");
  try {
    return JSON.parse(dec.decode(await crypto.subtle.decrypt({ name: "AES-GCM", iv: unb64(iv) }, await aesKey(keyB64), unb64(ct))));
  } catch { throw new Error("Cannot decrypt stored secret; check ENCRYPTION_KEY"); }
}
export const mask = (v) => (!v ? "" : "\u2022".repeat(8) + (String(v).length >= 12 ? String(v).slice(-4) : ""));
export const maskMap = (m) => Object.fromEntries(Object.entries(m).filter(([, v]) => v).map(([k, v]) => [k, mask(v)]));

// ---- sessions ----
const SESSION_TTL = 12 * 3600;
export async function makeSession(secret, user) {
  const csrf = b64url(crypto.getRandomValues(new Uint8Array(18)));
  const payload = b64url(enc.encode(JSON.stringify({ u: user, exp: Math.floor(Date.now() / 1000) + SESSION_TTL, csrf })));
  return { token: `${payload}.${await hmacHex(secret, payload)}`, csrf };
}
export async function readSession(secret, token) {
  if (!token || !token.includes(".") || !secret) return null;
  const i = token.lastIndexOf(".");
  const payload = token.slice(0, i), sig = token.slice(i + 1);
  if (!safeEqual(sig, await hmacHex(secret, payload))) return null;
  try {
    const d = JSON.parse(dec.decode(unb64url(payload)));
    return d.exp > Date.now() / 1000 ? d : null;
  } catch { return null; }
}

// ---- secret scrubbing for logs/errors ----
const PATTERNS = [/github_pat_[A-Za-z0-9_]{20,}/g, /gh[pousr]_[A-Za-z0-9]{20,}/g, /sk-[A-Za-z0-9_-]{16,}/g, /AIza[0-9A-Za-z_-]{20,}/g,
  /(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}/gi, /((?:api[_-]?key|access_token|token|secret|password)=)[^&\s"']{4,}/gi];
export function scrub(text, known = []) {
  let s = String(text ?? "");
  for (const k of known) if (k && String(k).length >= 6) s = s.split(String(k)).join("***");
  for (const p of PATTERNS) s = s.replace(p, (m, g1) => (g1 ? `${g1}***` : "***"));
  return s;
}
