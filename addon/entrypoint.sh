#!/bin/sh
set -e
# Wait for the database, apply migrations (never drops data), then start a single app process.
# The queue worker lives inside the app process; run exactly ONE replica of this service.
echo "Applying database migrations..."
i=0
until alembic upgrade head; do
  i=$((i+1)); [ "$i" -ge 30 ] && { echo "database not reachable"; exit 1; }
  echo "database not ready, retrying ($i)"; sleep 2
done
exec uvicorn app.main:app --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips='*'
