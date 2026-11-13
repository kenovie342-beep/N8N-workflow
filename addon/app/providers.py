import requests
from .security import decrypt

def test_llm(provider: str, model: str, endpoint: str, encrypted_key: str):
    key = decrypt(encrypted_key)
    p = provider.lower()
    try:
        if p in ("openai", "deepseek", "openai-compatible", "custom"):
            url = (endpoint or ("https://api.openai.com/v1")).rstrip("/") + "/chat/completions"
            r = requests.post(url, headers={"Authorization": f"Bearer {key}"},
                json={"model":model,"messages":[{"role":"user","content":"Reply with OK"}],"max_tokens":8}, timeout=30)
            r.raise_for_status()
            return {"ok":True,"message":"Connection successful","model":model}
        if p == "gemini":
            base = endpoint or "https://generativelanguage.googleapis.com/v1beta"
            url = f"{base.rstrip('/')}/models/{model}:generateContent?key={key}"
            r = requests.post(url,json={"contents":[{"parts":[{"text":"Reply with OK"}]}]},timeout=30)
            r.raise_for_status()
            return {"ok":True,"message":"Connection successful","model":model}
        if p == "anthropic":
            url = (endpoint or "https://api.anthropic.com/v1").rstrip("/") + "/messages"
            r = requests.post(url,headers={"x-api-key":key,"anthropic-version":"2023-06-01"},
                json={"model":model,"max_tokens":8,"messages":[{"role":"user","content":"Reply with OK"}]},timeout=30)
            r.raise_for_status()
            return {"ok":True,"message":"Connection successful","model":model}
        return {"ok":False,"message":"Unsupported provider"}
    except requests.RequestException as exc:
        return {"ok":False,"message":str(exc)[:400]}
