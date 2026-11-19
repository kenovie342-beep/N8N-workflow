# Serverless edition (Cloudflare Worker + D1 + GitHub Actions) - STATUS: stages 1-2 of 4 done

No always-on server. A Cloudflare Worker (free plan) runs a 1-minute cron that: fires due schedules, keeps n8n running inside GitHub Actions during
your windows (default 08:20-10:00, 13:20-15:00, 18:20-20:00 Africa/Lagos), recovers interrupted jobs and executes queued jobs within the concurrency cap.

## Stage 1 (done, tested): core
D1 schema and migration, AES-GCM secret storage, login/session/CSRF/rate-limit, persistent job queue with **atomic** concurrency cap,
priority, retries with backoff, Retry-After, dedupe, cancel/retry, lease recovery, cron scheduler with IANA timezones (default Africa/Lagos),
n8n window watchdog + heartbeat, GitHub dispatch/cancel/log client, HMAC-signed runner callbacks with replay protection, n8n token endpoints.
Tests: `npm test` (49 tests, real D1 + workerd through Miniflare). See the repo-level `docs/TEST_REPORT.md` for what is/was not verified.

## Stage 2 (done, tested): LLM + social
LLM providers (DeepSeek, OpenAI, Gemini, Anthropic, OpenAI-compatible, custom/local) with AES-GCM-encrypted keys, masked API, default provider, per-workflow
provider/model, real connection test. The 8 social modules (Reddit, Facebook, Instagram, YouTube, X, LinkedIn, Threads, Pinterest) in JavaScript with
credential validation, connection tests, publishing (+ media flows), error/rate-limit classification, retries. Tasks: `llm_prompt`, `social_publish`, `ai_publish`.
The 8 n8n workflows (`n8n-workflows/`) are contract-tested against this Worker. 81 tests; **all outbound HTTP is mocked - no live API call has been made.**
YouTube uploads from the Worker are limited to ~60 MB (use the GitHub runner for larger files).

## Not built yet
- Stage 3: self-hosting n8n in GitHub Actions (`n8n-host.yml`, state save/restore, tunnel, heartbeat sender).
- Stage 4: the dashboard UI (currently a placeholder page) and deployment docs.

## Deploy (when all stages are done)
```
npm install
npx wrangler d1 create suite            # put database_id in wrangler.toml
npm run migrate
npx wrangler secret put ADMIN_PASSWORD  # also: ADMIN_USERNAME SESSION_SECRET ENCRYPTION_KEY(base64 32 bytes) CALLBACK_SECRET N8N_ADDON_API_TOKEN
npm run deploy
```
Free-plan note: Workers allow limited subrequests/D1 queries per invocation; `MAX_JOBS_PER_TICK` (default 10) bounds work per cron tick.
