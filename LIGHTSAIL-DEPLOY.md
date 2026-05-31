# worldview — AWS Lightsail all-in-one deploy runbook

Puts the **frontend + backend + database on one Lightsail instance** (Option 1
from `../AWS-lightsail-1.md`). Caddy terminates TLS and serves the static
frontend at `jarvisworlds.com`/`www`; the FastAPI service lives at
`api.jarvisworlds.com`; Postgres stays on the internal compose network only.
No load balancer.

```
        Internet
           │  DNS A: jarvisworlds.com, www, api  → static IP
           ▼
   ┌──────────────────────────────────────────────────┐
   │ Lightsail instance  (Ubuntu 24.04, $7/2 GB)       │
   │  Caddy :80/:443  (free Let's Encrypt TLS)         │
   │   ├─ jarvisworlds.com, www → /srv/frontend (dist/)│
   │   └─ api.jarvisworlds.com  → api:8088             │
   │  api (FastAPI :8088, internal)                    │
   │  ingest (run_all.py every 15 min)                 │
   │  db (Postgres17 + PostGIS + pgvector,             │
   │      internal only, NOT public)                   │
   └──────────────────────────────────────────────────┘
```

This repo already contains the config changes for this layout:
- `compose.yaml` — Caddy mounts `../worldview/dist` at `/srv/frontend`.
- `Caddyfile` — apex/`www` serve the SPA; `api.` reverse-proxies the API.
- `.env.production.example` — `DOMAIN`, `CORS_ORIGINS` (apex+www), `SUMMARIZER_ENABLED=false`.

Run the steps below **on the box** unless noted "(local/CI)".

---

## 1. Provision the instance (Lightsail console)

1. Create instance → Linux → **Ubuntu 24.04 LTS** → **$7 / 2 GB / 2 vCPU** plan.
2. Networking → create a **static IP**, attach it to the instance.
3. Networking → Firewall → allow **22, 80, 443 only**. Do NOT add 5432.
   Lock 22 to your admin IP if possible.

## 2. Prepare the host

```bash
ssh ubuntu@<static-ip>

# 2 GB swap (absorbs embed/cluster RAM bursts; survives reboot)
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# Docker Engine + compose plugin
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker ubuntu   # log out/in for group to take effect

# Get the code (worldview and worldview-api MUST be siblings — the Caddy
# volume mounts ../worldview/dist relative to worldview-api/compose.yaml)
git clone <repo> ~/jarvis && cd ~/jarvis
```

## 3. DNS

Point all three names at the static IP, then wait for them to resolve before
starting the stack (Caddy needs them live to issue certs):

```
A   jarvisworlds.com        → <static-ip>
A   www.jarvisworlds.com    → <static-ip>
A   api.jarvisworlds.com    → <static-ip>
```

```bash
dig +short jarvisworlds.com www.jarvisworlds.com api.jarvisworlds.com   # all should print the static IP
```

## 4. Backend / DB config

```bash
cd ~/jarvis/worldview-api
cp .env.production.example .env
```

Edit `.env` and set at minimum:
- `DOMAIN=jarvisworlds.com`
- `POSTGRES_PASSWORD=<strong-random>`
- `POSTGRES_DB=worldview_prod`
- `DATABASE_URL=postgresql://worldview:<POSTGRES_PASSWORD>@db:5432/worldview_prod`
- `CORS_ORIGINS=https://jarvisworlds.com,https://www.jarvisworlds.com`   ← frontend origins
- `SUMMARIZER_ENABLED=false`   (already the default; keeps Anthropic spend $0)

`db` is internal-only (`expose: 5432`, not `ports:`) — leave it that way.

## 5. Build the frontend (local/CI or on the box)

The SPA reads `VITE_API_BASE` at build time. Point it at the API subdomain:

```bash
cd ~/jarvis/worldview
VITE_API_BASE=https://api.jarvisworlds.com npm ci && \
VITE_API_BASE=https://api.jarvisworlds.com npm run build
# produces worldview/dist — Caddy serves it via the compose mount
```

This `dist/` was already built locally against `https://api.jarvisworlds.com`,
so you can instead `rsync` it up and skip the box-side build:

```bash
# from your laptop, in the repo root:
rsync -az worldview/dist/ ubuntu@<static-ip>:~/jarvis/worldview/dist/
```

Building on a 2 GB box works but is tight; building in CI/locally and copying
`dist/` over (rsync/scp) keeps the box lean.

## 6. Launch & verify

```bash
cd ~/jarvis/worldview-api
docker compose up -d --build

docker compose ps           # caddy, api, ingest, db all "running"/"healthy"
docker compose logs caddy   # look for successful certificate obtains
curl -fsS https://api.jarvisworlds.com/health    # {"status":"ok",...}
```

Then in a browser load `https://jarvisworlds.com` — the globe should render and
events / search / markets should populate (they hit `api.jarvisworlds.com`).

Confirm Postgres is NOT reachable from outside:
```bash
# from your laptop, NOT the box — this must fail/timeout:
nc -vz <static-ip> 5432
```

## 7. Backups (durability is yours in Option 1)

1. Lightsail console → instance → Snapshots → **enable automatic daily snapshots**.
2. Create a Lightsail **bucket** (or use an existing S3 bucket) for off-box dumps.
3. Nightly `pg_dump` off-box (run inside the db container, copy out):

```bash
# /etc/cron.d/worldview-pgdump  (adjust bucket + creds)
0 3 * * * ubuntu docker compose -f /home/ubuntu/jarvis/worldview-api/compose.yaml exec -T db \
  pg_dump -U worldview worldview_prod | gzip > /home/ubuntu/backups/app-$(date +\%F).sql.gz \
  && aws s3 cp /home/ubuntu/backups/app-$(date +\%F).sql.gz s3://<bucket>/worldview/
```

### Restore runbook

- **Whole machine:** Lightsail → Snapshots → create a new instance from the
  latest snapshot → re-attach the static IP. DNS is unchanged, so you're back
  in minutes.
- **Database only** (from a `pg_dump`):
  ```bash
  gunzip -c app-YYYY-MM-DD.sql.gz | \
    docker compose exec -T db psql -U worldview -d worldview_prod
  ```
- Note: schema init SQL in `sql/` only runs on a **fresh** `db-data` volume.
  Wiping the volume destroys data — snapshot first. There is **no PITR**
  (WAL archiving is out of scope); the dump cadence bounds data loss to ≤24 h.

## 8. Cutover & rollback

- **Cutover:** once verification passes, this box is production. (DNS already
  points here from step 3.)
- **Rollback:** the frontend's Cloudflare path (`worldview/wrangler.jsonc`)
  remains available. If the box misbehaves, repoint DNS to Cloudflare Pages +
  a prior API host, or restore the latest Lightsail snapshot.
- Retire the Cloudflare **production** deploy for the frontend once the box is
  trusted (stop running `npm run deploy` in `worldview/`; keep the config as a
  documented fallback).

---

## Cost (realistic)

| Item | Cost |
|---|---|
| Lightsail $7 / 2 GB instance | $7/mo |
| Static IP, TLS, DNS, firewall | $0 |
| Load balancer | **not used** ($0) |
| Daily snapshots | ~$1–3/mo |
| Bucket for pg_dump | ~$1/mo |
| **Total** | **~$7–15/mo** |

Graduate to a managed Postgres (Lightsail Option 2 / RDS) when the data matters
and you want managed backups, HA, or PITR.
