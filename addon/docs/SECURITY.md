# Security and deployment

- Set unique long values for `ADMIN_PASSWORD`, `APP_SECRET_KEY`, `APP_ENCRYPTION_KEY`,
  `N8N_ADDON_API_TOKEN`, and `CALLBACK_SECRET`.
- Generate Fernet key with `Fernet.generate_key()`. Keep it backed up securely. Losing it
  makes saved API credentials unrecoverable.
- Do not commit `.env`. Use a secret manager in production.
- Put the service behind HTTPS and an access proxy (Cloudflare Access is suitable).
- The app uses HTTP Basic authentication for its admin UI/API. Use TLS; Basic auth is not
  safe over plain HTTP.
- n8n-to-suite calls use `X-Addon-Token`.
- GitHub callback uses `X-Callback-Secret`.
- Restrict GitHub token to the target repository and Actions workflow dispatch.
- Social platform credentials must be issued by the platform's official developer console.
- Review platform API terms, permissions, app review and rate limits.
- Never put API keys in n8n workflow JSON, Git commits, GitHub workflow inputs or logs.
