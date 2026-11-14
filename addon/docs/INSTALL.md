# Installation (Docker, recommended)

1. `cp .env.example .env` and fill every value. Generate secrets:
   - `openssl rand -hex 24` (Postgres password, CALLBACK_SECRET, N8N_ADDON_API_TOKEN)
   - `python3 -c "import secrets;print(secrets.token_urlsafe(48))"` (APP_SECRET_KEY)
   - `python3 -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"` (APP_ENCRYPTION_KEY - **back it up**; without it saved API keys cannot be decrypted)
2. Find the Docker network your n8n uses (`docker network ls`) and set `N8N_DOCKER_NETWORK` in `.env`.
   No n8n yet on that network? Remove the `n8n_net` network lines from `docker-compose.yml`.
3. `docker compose up -d --build`
4. Open `http://localhost:8080`, sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`.
5. In the UI: **AI Providers** (add + Test), **Social Accounts**, **GitHub & Cloudflare**, **Settings** (concurrency).

The container applies migrations on every start (`alembic upgrade head`); this never drops data.
The app refuses to start in production while any secret is a placeholder.

**Run exactly one `suite` container.** The queue worker runs inside it; the concurrency limit is enforced in the
database (Postgres advisory lock), but the scheduler/worker design is intended for a single instance.

## Without Docker
`pip install -r requirements.txt`, export the variables from `.env`, `alembic upgrade head`,
`uvicorn app.main:app --host 0.0.0.0 --port 8080`. SQLite works (`DATABASE_URL=sqlite:////data/suite.db`) but Postgres is advised.

## Backup and recovery
- Backup: `docker compose exec db pg_dump -U automation automation | gzip > suite-$(date +%F).sql.gz`
- Also back up `.env` (especially `APP_ENCRYPTION_KEY`).
- Restore: `gunzip -c suite-DATE.sql.gz | docker compose exec -T db psql -U automation automation` (into an empty database).
- Upgrades: `git pull && docker compose up -d --build` (migrations run automatically). Named volume `suite_pgdata` persists data; `docker compose down` keeps it, `down -v` deletes it.
- The add-on never touches n8n's volumes or database.
