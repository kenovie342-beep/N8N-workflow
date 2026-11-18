// Task runner + pump. Credentialed tasks (LLM/social) are added in stage 2.
import { UpstreamError, dispatchJob, githubConfig } from "./github.js";
import { addLog, claim, complete, fail, recoverExpired } from "./queue.js";
import { sleep } from "./util.js";

export class Deferred { constructor(timeoutMs) { this.timeoutMs = timeoutMs; } }
export const USER_TASKS = ["noop", "sleep", "github_job"];
export const REGISTRY = {};                       // stage 2 registers llm_prompt / social_publish / ai_publish here

const BUILTIN = {
  async noop() { return { ok: true }; },
  async sleep(payload) {                           // test/maintenance task; records observed parallelism
    const g = globalThis;
    g.__active = (g.__active || 0) + 1;
    g.__peak = Math.max(g.__peak || 0, g.__active);
    const peakSeen = g.__peak;
    try { await sleep(Math.min(Number(payload.ms ?? 100), 20000)); } finally { g.__active--; }
    if (payload.fail) throw new Error(String(payload.fail));
    return { slept_ms: payload.ms ?? 100, peak_in_isolate: peakSeen };
  },
  async github_job(payload, ctx) {
    await dispatchJob(await githubConfig(ctx.db, ctx.env), ctx.jobId);
    await addLog(ctx.db, ctx.jobId, "Dispatched to GitHub Actions; waiting for signed callback");
    return new Deferred(Number(ctx.env.GITHUB_JOB_TIMEOUT_SECONDS || 1800) * 1000);
  },
};

async function runOne(env, db, job) {
  const ctx = { db, env, jobId: job.id, attempts: job.attempts };
  try {
    const fn = REGISTRY[job.task] || BUILTIN[job.task];
    if (!fn) throw Object.assign(new Error(`Unknown task: ${job.task}`), { permanent: true });
    const res = await fn(JSON.parse(job.payload_json), ctx);
    if (res instanceof Deferred) {
      await db.prepare("UPDATE jobs SET lease_expires_at = ? WHERE id = ? AND status = 'running'").bind(Date.now() + res.timeoutMs, job.id).run();
      return;
    }
    await complete(db, job.id, res && typeof res === "object" ? res : { result: res });
  } catch (e) {
    const retryable = e instanceof UpstreamError ? e.retryable : !e.permanent && !(e instanceof TypeError && /JSON/.test(e.message));
    await fail(db, job.id, `${e.name}: ${e.message}`, { retryable, retryAfter: e.retryAfter || 0 });
  }
}

/** One scheduling cycle: recover interrupted jobs, claim within the concurrency cap, execute in parallel. */
export async function pump(env, db, { perTick = Number(env.MAX_JOBS_PER_TICK || 10) } = {}) {
  await recoverExpired(db);
  const jobs = await claim(db, env, { perTick });
  await Promise.allSettled(jobs.map((j) => runOne(env, db, j)));
  return jobs.length;
}
