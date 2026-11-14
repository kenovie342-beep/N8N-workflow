"""LLM providers: DeepSeek, OpenAI, Gemini, Anthropic, OpenAI-compatible, custom/local endpoints."""
from . import http

OPENAI_STYLE = {"openai", "deepseek", "openai-compatible", "custom", "local"}
PROVIDERS = sorted(OPENAI_STYLE | {"gemini", "anthropic"})
DEFAULT_ENDPOINTS = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "anthropic": "https://api.anthropic.com/v1",
}


def build_request(provider: str, model: str, endpoint: str, key: str, prompt: str,
                  system: str = "", max_tokens: int = 1024) -> tuple[str, dict, dict]:
    p = provider.lower()
    base = (endpoint or DEFAULT_ENDPOINTS.get(p, "")).rstrip("/")
    if not base:
        raise http.PermanentError("An endpoint is required for this provider")
    if p in OPENAI_STYLE:
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        headers = {"Content-Type": "application/json"}
        if key:  # local models often need no key
            headers["Authorization"] = f"Bearer {key}"
        return f"{base}/chat/completions", headers, {"model": model, "messages": msgs, "max_tokens": max_tokens}
    if p == "gemini":
        body: dict = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        # Key goes in a header, never in the URL (URLs leak into logs/error messages).
        return (f"{base}/models/{model}:generateContent",
                {"x-goog-api-key": key, "Content-Type": "application/json"}, body)
    if p == "anthropic":
        body = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
        if system:
            body["system"] = system
        return (f"{base}/messages",
                {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}, body)
    raise http.PermanentError(f"Unsupported provider: {provider}")


def parse_response(provider: str, data: dict) -> str:
    p = provider.lower()
    try:
        if p in OPENAI_STYLE:
            return data["choices"][0]["message"]["content"] or ""
        if p == "gemini":
            return "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"])
        if p == "anthropic":
            return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
    except (KeyError, IndexError, TypeError) as exc:
        raise http.PermanentError(f"Unexpected {provider} response shape: {type(exc).__name__}")
    raise http.PermanentError(f"Unsupported provider: {provider}")


def generate(provider: str, model: str, endpoint: str, key: str, prompt: str,
             system: str = "", max_tokens: int = 1024) -> str:
    url, headers, body = build_request(provider, model, endpoint, key, prompt, system, max_tokens)
    resp = http.request("POST", url, headers=headers, json=body, secrets=(key,), timeout=90)
    return parse_response(provider, resp.json())


def test_connection(provider: str, model: str, endpoint: str, key: str) -> dict:
    """Make a tiny real request. Never raises; returns {ok, message}."""
    try:
        text = generate(provider, model, endpoint, key, "Reply with the single word: OK", max_tokens=16)
        return {"ok": True, "message": "Connection successful", "model": model, "sample": text[:40]}
    except (http.RetryableError, http.PermanentError) as exc:
        return {"ok": False, "message": str(exc)[:400]}
