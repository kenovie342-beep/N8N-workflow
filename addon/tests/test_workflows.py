"""Validates the generated n8n workflows structurally and against the REAL n8n source checkout (if present)."""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WF = sorted((ROOT / "n8n-workflows").glob("*.json"))
N8N_SRC = Path(os.environ.get("N8N_SRC", "/home/claude/n8n-src"))
PLATFORMS = {"reddit", "facebook", "instagram", "youtube", "x", "linkedin", "threads", "pinterest"}
# node type -> directory name under packages/nodes-base/nodes
NODE_DIRS = {"manualTrigger": "ManualTrigger", "scheduleTrigger": "Schedule", "set": "Set", "httpRequest": "HttpRequest",
             "code": "Code", "wait": "Wait", "if": "If", "noOp": "NoOp"}


def test_eight_workflows_one_per_platform():
    assert {p.stem for p in WF} == PLATFORMS


@pytest.mark.parametrize("path", WF, ids=lambda p: p.stem)
def test_workflow_structure_is_valid_and_not_placeholder(path):
    w = json.loads(path.read_text())
    names = {n["name"] for n in w["nodes"]}
    assert len(names) == len(w["nodes"]), "duplicate node names"
    ids = [n["id"] for n in w["nodes"]]
    assert len(set(ids)) == len(ids)
    for src, outs in w["connections"].items():
        assert src in names
        for branch in outs["main"]:
            for link in branch:
                assert link["node"] in names, f"{src} -> missing {link['node']}"
    types = {n["type"].split(".")[-1] for n in w["nodes"]}
    assert {"manualTrigger", "scheduleTrigger", "httpRequest", "code", "wait", "if"} <= types
    assert w["settings"]["timezone"] == "Africa/Lagos"
    # every http node has a real URL and auth, no empty placeholder operations
    for n in w["nodes"]:
        if n["type"].endswith("httpRequest"):
            assert n["parameters"]["url"].startswith("=") and "/api/n8n/" in n["parameters"]["url"]
            assert n["parameters"]["genericAuthType"] == "httpHeaderAuth"
        if n["type"].endswith("code"):
            assert len(n["parameters"]["jsCode"]) > 80
    # secrets are never embedded and $env is not used (blocked by default in n8n 2.x)
    blob = path.read_text()
    assert "$env" not in blob and "sk-" not in blob and "github_pat_" not in blob
    # all nodes reachable from a trigger
    reach, todo = set(), ["Manual Trigger", "Schedule Trigger"]
    while todo:
        cur = todo.pop()
        if cur in reach:
            continue
        reach.add(cur)
        for branch in w["connections"].get(cur, {}).get("main", []):
            todo += [x["node"] for x in branch]
    assert reach == names


@pytest.mark.parametrize("path", WF, ids=lambda p: p.stem)
def test_code_node_javascript_parses_and_formats_correctly(path):
    w = json.loads(path.read_text())
    node = next(n for n in w["nodes"] if n["type"].endswith("code"))
    limit = int(re.search(r"LIMIT = (\d+)", node["parameters"]["jsCode"]).group(1))
    harness = (
        "const $input={first:()=>({json:{text:'x'.repeat(%d),provider:'p',model:'m'}})};"
        "const f=()=>{%s};const out=f();"
        "if(out[0].json.text.length!==%d)throw new Error('len '+out[0].json.text.length);"
        "console.log('ok')" % (limit + 500, node["parameters"]["jsCode"], limit)
    )
    r = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr


@pytest.mark.parametrize("path", WF, ids=lambda p: p.stem)
def test_n8n_expressions_are_syntactically_valid_js(path):
    w = json.loads(path.read_text())
    for n in w["nodes"]:
        for key in ("jsonBody", "url"):
            v = n["parameters"].get(key)
            if isinstance(v, str) and v.startswith("="):
                for expr in re.findall(r"\{\{(.*?)\}\}", v, flags=re.S):
                    r = subprocess.run(["node", "-e", "new Function('$json','$','return (' + process.argv[1] + ')')", expr],
                                       capture_output=True, text=True)
                    assert r.returncode == 0, f"{n['name']}.{key}: {r.stderr[:200]}"


@pytest.mark.skipif(not N8N_SRC.exists(), reason="n8n source checkout not available")
@pytest.mark.parametrize("path", WF, ids=lambda p: p.stem)
def test_node_types_and_versions_exist_in_real_n8n_source(path):
    w = json.loads(path.read_text())
    base = N8N_SRC / "packages" / "nodes-base" / "nodes"
    for n in w["nodes"]:
        short = n["type"].split(".")[-1]
        d = base / NODE_DIRS[short]
        assert d.exists(), f"{n['type']} not found in n8n source"
        text = "\n".join(p.read_text(errors="ignore") for p in d.rglob("*.ts") if "test" not in str(p).lower())
        declared = set()
        for m in re.finditer(r"(?:version|defaultVersion|currentVersion)\s*[:=]\s*(\[[^\]]+\]|[\d.]+)", text):
            declared |= {float(x) for x in re.findall(r"\d+(?:\.\d+)?", m.group(1))}
        assert float(n["typeVersion"]) in declared, f"{n['type']} v{n['typeVersion']} not in {sorted(declared)}"
