// GitHub Actions client. Only non-secret values are ever sent as workflow inputs.
import { scrub } from "./crypto.js";
import { decryptJSON } from "./crypto.js";

const API = "https://api.github.com";
import { UpstreamError } from "./http.js";
export { UpstreamError };

export async function githubConfig(db, env) {
  const row = await db.prepare("SELECT config_json, secret_enc FROM settings WHERE kind = 'github' LIMIT 1").first();
  const cfg = { owner: "", repo: "", workflow: "remote-job.yml", n8n_workflow: "n8n-host.yml", ref: "main", token: "" };
  if (row) Object.assign(cfg, JSON.parse(row.config_json), await decryptJSON(env.ENCRYPTION_KEY, row.secret_enc));
  return cfg;
}

async function gh(cfg, method, path, body, fetchFn = fetch) {
  if (!cfg.token || !cfg.owner || !cfg.repo) throw new UpstreamError("GitHub Actions is not configured (Settings -> GitHub)", false);
  let res;
  try {
    res = await fetchFn(`${API}/repos/${cfg.owner}/${cfg.repo}${path}`, { method, body: body ? JSON.stringify(body) : undefined,
      headers: { authorization: `Bearer ${cfg.token}`, accept: "application/vnd.github+json", "x-github-api-version": "2022-11-28", "user-agent": "suite-worker", "content-type": "application/json" } });
  } catch (e) { throw new UpstreamError(`Network error: ${scrub(e.message, [cfg.token])}`, true); }
  if (res.status >= 400) {
    const text = scrub((await res.text()).slice(0, 300), [cfg.token]);
    const retryable = res.status === 429 || res.status >= 500;
    throw new UpstreamError(`GitHub HTTP ${res.status}${res.status === 401 || res.status === 403 ? " (check token permissions)" : ""}: ${text}`, retryable, Number(res.headers.get("retry-after")) || 0);
  }
  return res;
}

export const runTitle = (jobId) => `suite-job ${jobId}`;
export async function dispatchJob(cfg, jobId) {
  const r = await gh(cfg, "POST", `/actions/workflows/${cfg.workflow}/dispatches`, { ref: cfg.ref, inputs: { job_id: jobId } });
  if (r.status !== 204) throw new UpstreamError(`Unexpected dispatch status ${r.status}`, false);
}
export async function dispatchN8n(cfg, windowEndIso) {
  const r = await gh(cfg, "POST", `/actions/workflows/${cfg.n8n_workflow}/dispatches`, { ref: cfg.ref, inputs: { window_end: windowEndIso } });
  if (r.status !== 204) throw new UpstreamError(`Unexpected dispatch status ${r.status}`, false);
}
export async function findRun(cfg, title) {
  const r = await gh(cfg, "GET", "/actions/runs?event=workflow_dispatch&per_page=50");
  return (await r.json()).workflow_runs.find((x) => x.display_title === title || x.name === title) || null;
}
export async function cancelRun(cfg, runId) { await gh(cfg, "POST", `/actions/runs/${runId}/cancel`); }
export async function runLogsUrl(cfg, runId) {
  const r = await gh(cfg, "GET", `/actions/runs/${runId}/logs`);
  return scrub(await r.text(), [cfg.token]).slice(0, 60000);
}
