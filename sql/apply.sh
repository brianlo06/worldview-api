#!/usr/bin/env bash
# Apply all SQL migrations in order. Idempotent.
set -euo pipefail

cd "$(dirname "$0")"
: "${DATABASE_URL:=postgresql://brianlo@localhost:5432/worldview_dev}"

for f in $(ls *.sql | sort); do
  echo "Applying $f..."
  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$f"
done

echo "Done."
