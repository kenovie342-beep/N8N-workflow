# Integrating with your existing n8n

Your n8n repository/containers stay as they are. The add-on talks to n8n only through HTTP.

## 1. Network
Put both on one Docker network (set `N8N_DOCKER_NETWORK` to n8n's network). From n8n the add-on is `http://suite:8080`.
If n8n is not in Docker, use the add-on's published URL (e.g. `http://host:8080` or your HTTPS domain).

## 2. n8n credential (once)
n8n -> Credentials -> *Header Auth*: name `Suite Add-on Token`, header name `X-Addon-Token`, value = `N8N_ADDON_API_TOKEN` from `.env`.
(The workflows use this credential, not `$env`, because n8n 2.x blocks environment access inside nodes by default.)

## 3. Import the workflows
n8n -> Workflows -> Import from file -> choose `n8n-workflows/<platform>.json` (8 files). Open the **HTTP Request** nodes and select the
credential from step 2. In the **Parameters** node set:
- `suite_url` (e.g. `http://suite:8080`)
- `account_id` = the id shown in the add-on's **Social Accounts** table
- optional `provider_id` / `model` (blank = default provider / its model) -> per-workflow LLM selection
- `topic`, `tone`, and `subreddit` / `media_url` where shown.

Flow: Manual Trigger or Schedule Trigger (disabled by default, timezone Africa/Lagos) -> Parameters -> **Generate content (LLM)** ->
**Format for <platform>** (length limits) -> **Queue publish job** -> Wait -> **Get job status** -> loops until completed/failed.
Execution history is n8n's own (Executions tab) plus the add-on's Jobs page.

## 4. Optional: keep the add-on inside your n8n repo
Copy this folder to `addon/` in your n8n clone. It is self-contained and does not modify any n8n file.

## 5. GitHub Actions runner
In the repository named under *Settings -> GitHub* (can be a separate small repo):
1. Copy `github-actions/remote-job.yml` to `.github/workflows/remote-job.yml`
2. Copy `github-actions/suite_runner.py` to `.github/scripts/suite_runner.py`
3. Repository secrets: `SUITE_URL` (public https URL of the add-on, or your Cloudflare Worker URL) and `CALLBACK_SECRET` (same as `.env`).
4. Create a fine-grained token limited to that repo with **Actions: read and write** and enter it in the UI (stored encrypted).
Only `job_id` is sent as a workflow input. The runner fetches its payload through a signed request and reports back through a signed callback.
Credentialed tasks (social, LLM) intentionally run on the add-on server so credentials never leave it; the runner executes allow-listed
credential-free tasks (`echo`, `sleep`, `http_check`). Extend `TASKS` in `suite_runner.py` for your own remote workloads.

## 6. Cloudflare Worker (optional but recommended for public callbacks)
```
cd worker && npm install
npx wrangler d1 create suite-control-plane     # copy database_id into wrangler.toml
npx wrangler d1 execute suite-control-plane --file=schema.sql --remote
npx wrangler r2 bucket create suite-media
npx wrangler secret put CALLBACK_SECRET        # same as .env
npx wrangler secret put ORIGIN_URL             # https URL of the add-on
npx wrangler deploy
```
Then:
- Add an optional repository secret `CALLBACK_URL` = `https://<your-worker>.workers.dev/callback`. The runner then posts results to the Worker, which verifies the
  signature, de-duplicates (D1), stores the callback and forwards it to the add-on (retrying every minute if the add-on is unreachable).
  Without `CALLBACK_URL` the runner posts directly to `$SUITE_URL/api/runner/callback`.
- `SUITE_URL` must still be the add-on's public https URL (the runner fetches its payload from it).
- Enter the Cloudflare account id, API token, Worker URL (and the shared secret) in the UI and press *Test connection*.
- The Worker's cron (every minute) nudges the add-on scheduler and retries undelivered callbacks. Signed R2 upload/download URLs: `POST /sign-url` (HMAC-signed request) returns a time-limited `/r2/<key>` link.
