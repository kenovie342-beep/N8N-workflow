# n8n Automation Suite — Companion Add-on

This is the add-on application for an n8n installation you already own. It does not bundle,
replace, or modify the original n8n source tree.

## Included
- Central Settings UI/API for LLM providers and social-platform credentials.
- Server-side Fernet encryption for stored API keys and tokens.
- LLM connection tests for OpenAI-compatible chat-completions endpoints.
- Eight social adapter registrations: Reddit, Facebook, Instagram, YouTube, X, LinkedIn,
  Threads and Pinterest. Each adapter has credential fields, capability metadata and a
  publishing interface that uses the platform's official API endpoints.
- Job queue, persisted job history, retry, cancel, configurable concurrency.
- Interval schedules with pause/resume/delete.
- GitHub Actions dispatch adapter and callback verification.
- PostgreSQL-ready persistence via SQLAlchemy; SQLite works for local testing.
- Docker Compose, health endpoint, API docs, tests and sample n8n workflow JSON exports.

## Start
1. Copy `.env.example` to `.env`.
2. Set a long random `APP_ENCRYPTION_KEY` (generate with
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
3. Set `ADMIN_USERNAME` and `ADMIN_PASSWORD`.
4. Set GitHub variables if you want remote execution.
5. Run `docker compose up -d --build`.
6. Open `http://localhost:8787`. Sign in with the admin credentials.
7. API docs: `http://localhost:8787/docs`.

## Add an LLM and API key
Settings → AI Providers → Add provider:
- Choose OpenAI, Gemini, Anthropic, DeepSeek or OpenAI-compatible.
- Enter model name, endpoint (if required), and API key.
- Save, then press Test connection.
- Choose a default provider/model. Workflows can reference a saved provider ID.

Keys are encrypted at rest using `APP_ENCRYPTION_KEY`; API responses only show masked values.
Never change the encryption key after saving credentials unless you first decrypt/export them.

## Add social API credentials
Settings → Social Accounts → select a platform and add its API credentials/tokens.
Only official platform APIs are used. Each platform requires its own developer app, review,
permissions and access tokens. Some platforms restrict publishing to approved accounts or
business profiles. A saved credential does not guarantee platform approval.

## n8n integration
Import JSON files from `n8n-workflows/` into your existing n8n instance. These are starter
webhook workflows that call this suite's authenticated API. Set `N8N_ADDON_API_TOKEN` in
both applications. They do not replace n8n's own workflow editor or native Schedule Trigger.

## GitHub Actions execution
Configure `GITHUB_TOKEN`, `GITHUB_OWNER`, `GITHUB_REPO`, `GITHUB_WORKFLOW_ID` and `GITHUB_REF`.
Copy `.github/workflows/remote-job.yml` into the target repository's `.github/workflows/`.
Set GitHub repository secrets `ADDON_CALLBACK_URL` and `ADDON_CALLBACK_SECRET`.
The callback URL must be reachable by GitHub-hosted runners (use a secure HTTPS tunnel or
Cloudflare deployment). Do not expose the service without HTTPS and authentication.

## Important limitations
- This is a complete companion application source package, not a modification to the n8n
  editor. Its management UI is a separate page at port 8787.
- Social APIs require real developer credentials and may require app review. The suite
  implements API adapters, but does not bypass platform approval, rate limits or policies.
- Remote Actions jobs run on temporary GitHub-hosted runners. They cannot host the always-on
  n8n editor/API.
- Cloudflare can proxy the service and provide HTTPS/access protection; this package does not
  create Cloudflare resources or deploy DNS/Workers automatically.
- LLM provider testing supports OpenAI-compatible chat completions. Gemini and Anthropic
  endpoints are normalized through their own API formats.
- Review docs/SECURITY.md before public deployment.
