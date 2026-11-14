#!/usr/bin/env python3
"""Generate the 8 importable n8n workflows (one per platform) into n8n-workflows/."""
import json
import uuid
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "n8n-workflows"
LIMITS = {"x": 280, "threads": 500, "linkedin": 3000, "instagram": 2200, "facebook": 63000, "reddit": 40000, "pinterest": 800, "youtube": 5000}
EXTRA = {  # platform-specific publish fields (JavaScript object literals evaluated inside n8n)
    "reddit": "{ subreddit: $('Parameters').item.json.subreddit, title: $json.title }",
    "facebook": "{}", "linkedin": "{}", "x": "{}", "threads": "{}",
    "instagram": "{ media_url: $('Parameters').item.json.media_url }",
    "pinterest": "{ media_url: $('Parameters').item.json.media_url, title: $json.title }",
    "youtube": "{ media_url: $('Parameters').item.json.media_url, media_type: 'video', title: $json.title }",
}
PARAMS = {  # extra fields to show in the Parameters node
    "reddit": [("subreddit", "your_subreddit")], "instagram": [("media_url", "https://example.com/image.jpg")],
    "pinterest": [("media_url", "https://example.com/image.jpg")], "youtube": [("media_url", "https://example.com/video.mp4")],
}
CRED = {"httpHeaderAuth": {"id": "REPLACE_ME", "name": "Suite Add-on Token"}}


def nid() -> str:
    return str(uuid.uuid4())


def http_node(name, pos, method, url, body=None):
    p = {"method": method, "url": url, "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
         "options": {"timeout": 120000}}
    if body is not None:
        p.update(sendBody=True, specifyBody="json", jsonBody=body)
    return {"id": nid(), "name": name, "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": pos,
            "parameters": p, "credentials": CRED}


def build(platform: str) -> dict:
    limit = LIMITS[platform]
    assigns = [("suite_url", "http://suite:8080", "string"), ("account_id", "PASTE_SOCIAL_ACCOUNT_ID", "string"),
               ("provider_id", "", "string"), ("model", "", "string"),
               ("topic", "Tips for growing a small business online", "string"),
               ("tone", "friendly and concise", "string")] + [(k, v, "string") for k, v in PARAMS.get(platform, [])]
    fields = [{"id": nid(), "name": n, "value": v, "type": t} for n, v, t in assigns]

    nodes = [
        {"id": nid(), "name": "Manual Trigger", "type": "n8n-nodes-base.manualTrigger", "typeVersion": 1, "position": [0, 0], "parameters": {}},
        {"id": nid(), "name": "Schedule Trigger", "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2, "position": [0, 200],
         "parameters": {"rule": {"interval": [{"field": "days", "daysInterval": 1, "triggerAtHour": 9}]}}, "disabled": True},
        {"id": nid(), "name": "Parameters", "type": "n8n-nodes-base.set", "typeVersion": 3.4, "position": [240, 100],
         "parameters": {"assignments": {"assignments": fields}, "options": {}}},
        http_node("Generate content (LLM)", [480, 100], "POST", "={{ $json.suite_url }}/api/n8n/generate",
                  '={{ JSON.stringify({ prompt: "Write a " + $json.tone + " " + "' + platform + '" + " post about: " + $json.topic + ". Output only the post text.", provider_id: $json.provider_id, model: $json.model, system: "You are a social media copywriter." }) }}'),
        {"id": nid(), "name": f"Format for {platform}", "type": "n8n-nodes-base.code", "typeVersion": 2, "position": [720, 100],
         "parameters": {"jsCode": (
             f"const LIMIT = {limit};\n"
             "let text = String($input.first().json.text || '').trim().replace(/\\n{3,}/g, '\\n\\n');\n"
             "if (text.length > LIMIT) text = text.slice(0, LIMIT - 1).trimEnd() + '\\u2026';\n"
             "const title = text.split('\\n')[0].slice(0, 100);\n"
             "return [{ json: { text, title, provider: $input.first().json.provider, model: $input.first().json.model } }];")}},
        http_node("Queue publish job", [960, 100], "POST", "={{ $('Parameters').item.json.suite_url }}/api/n8n/jobs",
                  '={{ JSON.stringify({ task: "social_publish", workflow: "n8n:' + platform + '", payload: Object.assign({ account_id: $(\'Parameters\').item.json.account_id, text: $json.text, raw: true }, ' + EXTRA[platform] + ') }) }}'),
        {"id": nid(), "name": "Wait", "type": "n8n-nodes-base.wait", "typeVersion": 1.1, "position": [1200, 100],
         "parameters": {"amount": 15, "unit": "seconds"}},
        http_node("Get job status", [1440, 100], "GET", "={{ $('Parameters').item.json.suite_url }}/api/n8n/jobs/{{ $('Queue publish job').item.json.id }}"),
        {"id": nid(), "name": "Finished?", "type": "n8n-nodes-base.if", "typeVersion": 2.2, "position": [1680, 100],
         "parameters": {"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
                                       "conditions": [{"id": nid(), "leftValue": "={{ ['completed','failed','cancelled'].includes($json.status) }}",
                                                       "rightValue": "", "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
                                       "combinator": "and"}, "options": {}}},
        {"id": nid(), "name": "Done", "type": "n8n-nodes-base.noOp", "typeVersion": 1, "position": [1920, 0], "parameters": {}},
    ]
    # Instagram/YouTube/Pinterest need media; the Format node text is passed via payload.text, EXTRA adds the rest.
    c = lambda a, b, i=0: {a: {"main": [[{"node": b, "type": "main", "index": i}]]}}
    conns = {}
    for a, b in [("Manual Trigger", "Parameters"), ("Schedule Trigger", "Parameters"), ("Parameters", "Generate content (LLM)"),
                 ("Generate content (LLM)", f"Format for {platform}"), (f"Format for {platform}", "Queue publish job"),
                 ("Queue publish job", "Wait"), ("Wait", "Get job status"), ("Get job status", "Finished?")]:
        conns.update(c(a, b))
    conns["Finished?"] = {"main": [[{"node": "Done", "type": "main", "index": 0}], [{"node": "Wait", "type": "main", "index": 0}]]}
    return {"name": f"Suite - AI post to {platform}", "nodes": nodes, "connections": conns, "active": False,
            "settings": {"executionOrder": "v1", "timezone": "Africa/Lagos", "saveManualExecutions": True,
                         "saveDataSuccessExecution": "all", "saveDataErrorExecution": "all"},
            "pinData": {}, "tags": [], "meta": {"templateCredsSetupCompleted": False}}


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for p in LIMITS:
        (OUT / f"{p}.json").write_text(json.dumps(build(p), indent=2) + "\n")
    print("wrote", len(LIMITS), "workflows")
