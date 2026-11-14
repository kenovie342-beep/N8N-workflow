# Configuration guide

## Environment variables (`.env`)
| Variable | Purpose |
|---|---|
| `DATABASE_URL` | set by docker-compose to the Postgres service |
| `APP_SECRET_KEY` | signs session cookies |
| `APP_ENCRYPTION_KEY` | Fernet key; encrypts every stored API key/token. Back it up. |
| `CALLBACK_SECRET` | HMAC secret shared with the GitHub runner and Cloudflare Worker |
| `N8N_ADDON_API_TOKEN` | token n8n sends as `X-Addon-Token` |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` or `ADMIN_PASSWORD_HASH` | dashboard login (prefer the PBKDF2 hash) |
| `PUBLIC_BASE_URL` | if `https://...`, session cookies are `Secure` |
| `MAX_CONCURRENT_JOBS` | initial concurrency (afterwards managed in the UI; stored in the DB) |
| `DEFAULT_TIMEZONE` | default `Africa/Lagos` |
| `GITHUB_JOB_TIMEOUT_SECONDS` | how long a dispatched job may wait for its callback before recovery (default 1800) |

## UI settings
- **AI Providers:** provider type, model, optional endpoint, key. *Test* makes a real 16-token request. Exactly one provider is the default.
  Workflows can choose another provider/model per run (`provider_id`, `model`). OpenAI-compatible, custom and local (e.g. Ollama `http://host:11434/v1`) need an endpoint.
- **Social Accounts:** one row per account; see `SOCIAL_PERMISSIONS.md`.
- **Settings:** max concurrency 1-100 (effective immediately, enforced by the queue's database claim), default timezone.
- **Schedules:** cron / one-time / interval, any IANA timezone (default Africa/Lagos), pause/resume/edit/delete/run-now, upcoming + history.

## Queue semantics
- Jobs: `queued -> running -> completed | failed | cancelled`. Higher `priority` first, then oldest.
- Failures: transient errors (network, 429, 5xx) retry with exponential backoff (5s doubling, max 15 min; `Retry-After` honoured) up to `max_attempts`; validation/permission errors fail immediately.
- A worker crash or lost callback is detected by lease expiry (local jobs: on restart; GitHub jobs: lease timeout + GitHub run-state check) and the job is retried or failed - never silently dropped.
- Cancel: queued -> cancelled at once; local running -> cooperative stop; GitHub running -> GitHub run cancelled.
- Duplicate prevention: `dedupe_key` (one *active* job per key); schedules use `sched:<id>:<slot>` so a slot fires exactly once.
- Misfires: a schedule overdue by more than 1 hour (e.g. long downtime) skips to its next slot instead of flooding the queue; one-time schedules always run.
- GitHub concurrency: a GitHub job occupies one of your N slots until its callback arrives. GitHub's own `concurrency:` groups are only used to stop the same job id running twice; GitHub's runner limits (plan dependent) still apply on top of your N.
