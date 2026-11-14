# Test report

Environment: Linux sandbox, Python 3.12, Node 22, **real PostgreSQL 16** and SQLite, real uvicorn processes, n8n source v2.48.0 (checkout of `n8n-io/n8n`).
Python tests that touch the database run on **both** SQLite and PostgreSQL. Everything below "PASSED" was actually executed.

## PASSED (executed)
| Area | What ran | Result |
|---|---|---|
| Static | `ruff check` (app, tests, tools, runner, migrations); `mypy app` | clean |
| Security (14) | Fernet round-trip + wrong key, masking, ciphertext at rest, session tamper/expiry, CSRF, login rate limit, PBKDF2 login, HMAC valid/stale/tampered/missing, nonce replay, log scrubbing, production refuses placeholder secrets | pass |
| LLM (13) | Request construction for DeepSeek/OpenAI/Gemini (key in header, not URL)/Anthropic/local; response parsing; connection test success/failure; key never leaked in errors; 429 Retry-After; save/list(masked)/update(keeps key)/default/delete via API; per-workflow model override | pass (HTTP **mocked**) |
| Queue/concurrency (36, both DBs) | limit 5 with 12 jobs: in-process peak == 5, DB-observed running never > 5, remainder stays queued, all 12 complete; runtime change of limit without restart; limits 10 and 20; 12 racing claimers never over-claim; priority/FIFO; dedupe; retry+backoff; Retry-After; permanent errors not retried; cancel queued/running; retry; crash recovery; lease expiry; persistence across engine restart | pass |
| Scheduler (23, both DBs) | create/edit/pause/resume/delete/run-now; Africa/Lagos default; cron->UTC conversion (Lagos, New York); one-time local->UTC; validation; exactly-once firing per slot; paused doesn't fire; misfire skipping; overdue-after-restart fires once; scheduled job executes through real worker; upcoming + history | pass |
| GitHub engine (32 tests incl. runner) | dispatch sends only `job_id`; dispatch failure classification; deferred job holds a slot; callbacks: valid/invalid/stale/tampered signature, replay, duplicate (new nonce) idempotent, late callback can't resurrect cancelled job, failed+retryable, payload endpoint signed/scoped; lost-callback failure detection; remote cancel; log retrieval + scrubbing. **Real runner script against a real local server**: success, disallowed task, wrong secret, logs stored | pass (GitHub API **mocked**) |
| Social (28) | All 8 platforms: connection test request/response, publish request construction + response parsing, media flows (FB photo/video, IG container+poll, Threads poll, X upload, LinkedIn image upload, YouTube resumable), error mapping (401/403 permanent, 429/5xx retryable with Retry-After), formatting limits, masked credential API, end-to-end job via worker (formatting enforced, retry on 429) | pass (**ALL HTTP MOCKED - no live publishing**) |
| Migrations (8) | upgrade on empty DB (SQLite+Postgres), idempotent re-run, data preserved, **models == migrations (no drift)**, downgrade | pass |
| Restart (3) | Real uvicorn process SIGKILLed mid-job and restarted on same DB: schedule, settings, encrypted key, jobs persisted; interrupted job recovered; second job took the free slot; no jobs lost. Production start refused with placeholder secrets | pass (SQLite + Postgres) |
| n8n workflows (33) | 8 workflows: structure, connections, reachability, no `$env`, no secrets, Code-node JS executed in Node (length limits), every expression parsed as JS, **node types and typeVersions present in the real n8n 2.48.0 source** | pass |
| UI/API (12) | dashboard served with security headers, JS parses, no demo data, stats from DB, n8n token auth, Cloudflare settings masked, `/api/cf/tick` signed, payload secrets masked | pass |
| Cloudflare Worker (8, Node) | signature/stale/tamper/replay, store+forward, outage -> 202 -> retried by cron, signed R2 mint/PUT/GET/expiry/tamper/traversal, no error leakage | pass (in-memory D1/R2 fakes) |
| Mutation checks | Raising the concurrency cap by 3 makes 4 queue tests fail; disabling signature verification makes the signature test fail | tests do detect these bugs |

**Totals:** 210 Python tests passed, 8 Node tests passed, 0 failed.

## NOT RUN (could not be, in this environment)
| Item | Why | What you need to do |
|---|---|---|
| `docker build` / `docker compose up`, container restart, health in containers | no Docker daemon in the sandbox | `docker compose up -d --build`; `docker compose restart suite`; `curl localhost:8080/health`. (The same flow was verified with real processes + Postgres, and compose/Dockerfile were only parsed, not built.) |
| Live LLM calls | no API keys supplied | UI -> AI Providers -> **Test** |
| Live social publishing / real platform auth | no platform credentials | UI -> Social Accounts -> **Test connection**, then a test post |
| Real GitHub Actions run (dispatch, runner, callback over the internet, remote cancel, log zip) | needs your repo + token + public URL | follow `docs/INTEGRATION.md` section 5, then queue a `github_job` |
| Cloudflare deploy (Wrangler, real D1/R2, cron) and token verify | needs your Cloudflare account | follow `docs/INTEGRATION.md` section 6 |
| Full n8n build, import and execution of the workflows | a full n8n monorepo build is far beyond the sandbox; only node types/versions and JS/expressions were checked against the source | import the JSON files into your n8n and run a manual execution |
| Linting of the JS Worker, frontend build | no frontend build step exists (plain HTML/JS); Worker not linted | - |
| Load testing beyond 20 simultaneous jobs | not attempted | - |

## Known limitations (by design / honest notes)
- Single app instance (worker + scheduler in-process). Concurrency claims are DB-serialised, but run one replica.
- Rate limiting and sessions are per process (in memory / stateless cookie).
- Social: see `SOCIAL_PERMISSIONS.md` for unimplemented media types. X's v2 media endpoint and LinkedIn's API version string are the least certain pieces; they are mock-tested against my reading of the docs, not live-verified.
- GitHub-run jobs execute only allow-listed credential-free tasks; credentialed tasks run on the add-on server.
