#!/usr/bin/env bash
#
# worldview — one-command box bootstrap for the Lightsail all-in-one deploy.
# Idempotent: safe to re-run. Performs runbook §2, §5-check, §6.
#
#   Prereqs (do these first — see LIGHTSAIL-DEPLOY.md):
#     - Lightsail instance + attached static IP (done: 3.139.182.3)
#     - DNS A records for jarvisworlds.com / www / api -> static IP (DNS-only)
#     - This repo cloned so `worldview` and `worldview-api` are siblings
#     - worldview/dist built (rsync it up, or this script reminds you)
#
#   Usage (from the box):
#     cd ~/jarvis/worldview-api
#     ./deploy.sh
#
set -euo pipefail

DOMAIN="jarvisworlds.com"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!!  %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mxx  %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Swap (runbook §2) — absorbs embed/cluster RAM bursts on the 2 GB box.
# ---------------------------------------------------------------------------
if ! sudo swapon --show | grep -q '/swapfile'; then
  say "Creating 2 GB swap file"
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
else
  say "Swap already active — skipping"
fi

# ---------------------------------------------------------------------------
# 2. Docker Engine + compose plugin (runbook §2).
# ---------------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker Engine + compose plugin"
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER" || true
  warn "Added $USER to the docker group. This script will use 'sudo docker' for now;"
  warn "log out and back in afterwards so 'docker' works without sudo."
else
  say "Docker already installed — skipping"
fi

# Decide whether we need sudo to talk to the docker daemon (group not active
# until re-login). Pick a working invocation for this run.
if docker info >/dev/null 2>&1; then
  DC="docker compose"
else
  DC="sudo docker compose"
fi

# ---------------------------------------------------------------------------
# 3. .env (runbook §4) — never overwrite an existing one.
# ---------------------------------------------------------------------------
if [[ ! -f .env ]]; then
  say "Creating .env from .env.production.example"
  cp .env.production.example .env
  cat <<EOF

  $(warn "Edit worldview-api/.env before continuing — set at minimum:")
    DOMAIN=${DOMAIN}
    POSTGRES_PASSWORD=<strong-random>
    POSTGRES_DB=worldview_prod
    DATABASE_URL=postgresql://worldview:<POSTGRES_PASSWORD>@db:5432/worldview_prod
    CORS_ORIGINS=https://${DOMAIN},https://www.${DOMAIN}
    SUMMARIZER_ENABLED=false

  Then re-run: ./deploy.sh
EOF
  exit 1
fi

# Sanity-check required .env values are filled.
grep -q "^DOMAIN=${DOMAIN}$"          .env || warn "DOMAIN in .env is not ${DOMAIN} — double-check it."
grep -Eq "^POSTGRES_PASSWORD=.+"      .env || die  "POSTGRES_PASSWORD is empty in .env — set it and re-run."
grep -Eq "^DATABASE_URL=.+"           .env || die  "DATABASE_URL is empty in .env — set it and re-run."
grep -q  "^SUMMARIZER_ENABLED=false$" .env || warn "SUMMARIZER_ENABLED is not false — Anthropic calls may incur cost."

# ---------------------------------------------------------------------------
# 4. Frontend build present (runbook §5) — Caddy mounts ../worldview/dist.
# ---------------------------------------------------------------------------
if [[ ! -f ../worldview/dist/index.html ]]; then
  die "../worldview/dist/index.html missing. Build/rsync the frontend first:
       (laptop) rsync -az worldview/dist/ ubuntu@3.139.182.3:~/jarvis/worldview/dist/
       or build on the box:
       (box) cd ../worldview && VITE_API_BASE=https://api.${DOMAIN} npm ci && \\
             VITE_API_BASE=https://api.${DOMAIN} npm run build"
fi
# Cheap guard against shipping a dev/wrong-origin build.
if ! grep -rq "https://api.${DOMAIN}" ../worldview/dist/assets/*.js 2>/dev/null; then
  warn "Did not find https://api.${DOMAIN} in the built JS — was dist built with the right VITE_API_BASE?"
fi

# ---------------------------------------------------------------------------
# 5. Launch (runbook §6).
# ---------------------------------------------------------------------------
say "Building and starting the stack (caddy + api + ingest + db)"
$DC up -d --build

say "Service status"
$DC ps

say "Waiting for the API /health (Caddy may take ~30s to issue the cert)"
for i in $(seq 1 30); do
  if curl -fsS "https://api.${DOMAIN}/health" >/dev/null 2>&1; then
    say "API healthy at https://api.${DOMAIN}/health"
    break
  fi
  sleep 5
  [[ $i -eq 30 ]] && warn "API /health not green yet — check: $DC logs caddy ; $DC logs api"
done

cat <<EOF

$(say "Done. Next:")
  - Open https://${DOMAIN} in a browser — the globe should render.
  - From your laptop (NOT the box), confirm Postgres is closed:  nc -vz 3.139.182.3 5432  (must fail)
  - Enable daily snapshots + the nightly pg_dump cron (LIGHTSAIL-DEPLOY.md §7).
EOF
