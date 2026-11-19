import test from "node:test";
import assert from "node:assert/strict";
import * as L from "../src/llm.js";
import { deps } from "./mock.mjs";

test("request construction per provider (Gemini key in header, local needs no key)", () => {
  let r = L.buildRequest({ provider: "deepseek", model: "deepseek-chat", endpoint: "", key: "KEY123", prompt: "hi", system: "sys" });
  assert.equal(r.url, "https://api.deepseek.com/v1/chat/completions"); assert.equal(r.headers.authorization, "Bearer KEY123");
  assert.deepEqual(r.body.messages[0], { role: "system", content: "sys" });
  assert.ok(L.buildRequest({ provider: "openai", model: "m", key: "K", prompt: "x" }).url.startsWith("https://api.openai.com/v1/"));
  r = L.buildRequest({ provider: "gemini", model: "gemini-2.0-flash", key: "GKEY", prompt: "hi" });
  assert.ok(!r.url.includes("GKEY")); assert.equal(r.headers["x-goog-api-key"], "GKEY"); assert.ok(r.url.endsWith(":generateContent"));
  r = L.buildRequest({ provider: "anthropic", model: "claude-x", key: "AKEY", prompt: "hi", system: "s" });
  assert.ok(r.url.endsWith("/v1/messages")); assert.equal(r.headers["x-api-key"], "AKEY"); assert.equal(r.body.system, "s");
  r = L.buildRequest({ provider: "local", model: "llama3", endpoint: "http://localhost:11434/v1", key: "", prompt: "hi" });
  assert.equal(r.headers.authorization, undefined); assert.equal(r.url, "http://localhost:11434/v1/chat/completions");
  assert.throws(() => L.buildRequest({ provider: "custom", model: "m", key: "k", prompt: "x" }), /endpoint/);
});
test("response parsing", () => {
  assert.equal(L.parseResponse("openai", { choices: [{ message: { content: "hey" } }] }), "hey");
  assert.equal(L.parseResponse("gemini", { candidates: [{ content: { parts: [{ text: "a" }, { text: "b" }] } }] }), "ab");
  assert.equal(L.parseResponse("anthropic", { content: [{ type: "text", text: "yo" }] }), "yo");
  assert.throws(() => L.parseResponse("openai", { nope: 1 }), /response shape/);
});
test("connection test success, failure without key leak, 429 Retry-After", async () => {
  const cfg = { provider: "deepseek", model: "deepseek-chat", endpoint: "", key: "KEY" };
  let d = deps([{ match: "https://api.deepseek.com/v1/chat/completions", respond: { choices: [{ message: { content: "OK" } }] } }]);
  const ok = await L.testConnection(cfg, d.fetchFn); assert.ok(ok.ok); assert.equal(ok.sample, "OK");
  assert.equal(d.calls[0].headers.authorization, "Bearer KEY"); assert.equal(JSON.parse(d.calls[0].body).model, "deepseek-chat");
  d = deps([{ match: "https://api.openai.com/v1/chat/completions", respond: new Response("bad key sk-LEAKYKEY1234567890", { status: 401 }) }]);
  const bad = await L.testConnection({ provider: "openai", model: "gpt", endpoint: "", key: "sk-LEAKYKEY1234567890" }, d.fetchFn);
  assert.equal(bad.ok, false); assert.ok(!bad.message.includes("sk-LEAKYKEY")); assert.match(bad.message, /401/);
  d = deps([{ match: "https://api.openai.com/v1/chat/completions", respond: new Response("slow", { status: 429, headers: { "retry-after": "42" } }) }]);
  await assert.rejects(L.generate({ provider: "openai", model: "m", endpoint: "", key: "k" }, "p", { fetchFn: d.fetchFn }), (e) => e.retryable && e.retryAfter === 42);
});
