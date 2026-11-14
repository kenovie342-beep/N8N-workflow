"""GitHub Actions dispatch / cancel / log retrieval / run-state lookup."""
import io
import zipfile

from . import http
from .logsafe import scrub

API = "https://api.github.com"


def _h(cfg: dict) -> dict:
    return {"Authorization": f"Bearer {cfg['token']}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _check(cfg: dict) -> None:
    if not all(cfg.get(k) for k in ("token", "owner", "repo")):
        raise http.PermanentError("GitHub Actions integration is not configured (Settings -> GitHub)")


def run_title(job_id: str) -> str:
    return f"suite-job {job_id}"  # must match `run-name` in the workflow


def dispatch(cfg: dict, job_id: str) -> None:
    """Only the job id is sent as an input - payloads and secrets never travel through workflow inputs."""
    _check(cfg)
    r = http.request("POST", f"{API}/repos/{cfg['owner']}/{cfg['repo']}/actions/workflows/{cfg['workflow']}/dispatches",
                     headers=_h(cfg), json={"ref": cfg["ref"], "inputs": {"job_id": job_id}}, secrets=(cfg["token"],))
    if r.status_code != 204:
        raise http.PermanentError(f"Unexpected dispatch response: {r.status_code}")


def find_run(cfg: dict, job_id: str) -> dict | None:
    _check(cfg)
    r = http.request("GET", f"{API}/repos/{cfg['owner']}/{cfg['repo']}/actions/runs",
                     headers=_h(cfg), params={"event": "workflow_dispatch", "per_page": 50}, secrets=(cfg["token"],))
    for run in r.json().get("workflow_runs", []):
        if run.get("display_title") == run_title(job_id) or run.get("name") == run_title(job_id):
            return run
    return None


def cancel_run(cfg: dict, run_id: str) -> None:
    _check(cfg)
    http.request("POST", f"{API}/repos/{cfg['owner']}/{cfg['repo']}/actions/runs/{run_id}/cancel",
                 headers=_h(cfg), secrets=(cfg["token"],))


def get_logs(cfg: dict, run_id: str, max_chars: int = 60000) -> str:
    _check(cfg)
    r = http.request("GET", f"{API}/repos/{cfg['owner']}/{cfg['repo']}/actions/runs/{run_id}/logs",
                     headers=_h(cfg), secrets=(cfg["token"],), timeout=60)
    try:
        z = zipfile.ZipFile(io.BytesIO(r.content))
    except zipfile.BadZipFile:
        return scrub(r.text[:max_chars])
    parts = [f"--- {n} ---\n" + z.read(n).decode("utf-8", "replace") for n in sorted(z.namelist()) if n.endswith(".txt")]
    return scrub("\n".join(parts))[:max_chars]
