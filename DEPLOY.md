# worldview — deployment plan

Target: get `worldview` (frontend globe) + `worldview-api` (FastAPI + Postgres
+ 15-min ingestion job) on the public internet behind an existing domain,
cheap and reliable, today.

---

## Current state (sized from local dev)

| Component | Detail |
|---|---|
| Frontend bundle | 820 KB JS / 222 KB gzipped + 2.8 MB static (textures, country borders) |
| Backend | FastAPI + uvicorn, Python 3.12 |
| Postgres | 299 MB total (raw_events 143 MB, events 100 MB, clusters 40 MB) — ~11.8k events, 9.1k clusters in ~24 h of ingestion |
| Postgres extensions required | `postgis`, `vector` (pgvector with HNSW), `pgcrypto` |
| Embedding model | `BAAI/bge-small-en-v1.5` via `fastembed` — ~130 MB ONNX file, runs on CPU |
| Ingestion cadence | `scripts/run_all.py` every 15 min (GDELT events + GKG + NWS + markets + currencies + enrich + embed + cluster + summarize + anomalies) |
| Peak RAM | ~600 MB during embedding + clustering bursts. Sustained < 250 MB |
| Outbound | Anthropic API (Claude Haiku), GDELT (CSV downloads), NOAA, Stooq, Frankfurter |

Growth assumption: ~150 MB DB / week → ~8 GB / year before pruning. Plan for
a Postgres tier with at least 10 GB headroom.

---

## "Why is there an Anthropic key, why is there pgvector, what's the embedding model?"

Three things often conflated; the worldview pipeline only pays money for one
of them, and that one is currently off.

### 1. Embeddings (free, local, always on)

Every event title gets converted into a 384-dimension vector so that
"similar" headlines end up close together in vector space. Worldview uses
**`BAAI/bge-small-en-v1.5`** via the [`fastembed`](https://github.com/qdrant/fastembed)
library — a CPU ONNX model that runs **inside the API process on the same
box, with zero external API calls**. It's ~130 MB on disk, ~250 MB resident
in memory, and embeds a batch of 100 titles in well under a second on a
2-vCPU droplet.

This is what powers semantic search ("trade tensions in asia") and the
deduplication that collapses 50 headlines about the same Trump–Xi meeting
into one cluster.

### 2. pgvector (free, Postgres extension)

pgvector is a **Postgres extension** that adds a `vector` column type plus
a fast HNSW index for nearest-neighbor queries. It is **not an external
service** — it ships as a `CREATE EXTENSION vector;` inside your own
database. No bill, no separate vendor. The vectors stored in there are
the same ones fastembed produced in step (1). The clustering worker queries
"give me the cluster centroid most similar to this new event's embedding"
in milliseconds against this index.

So: pgvector is the storage, fastembed is the model. Both are free and
local.

### 3. Anthropic / Claude (paid, currently OFF)

The only piece that ever costs money is the **cluster summarizer**, which
calls Claude Haiku 4.5 to rewrite a representative title into an AP-style
neutral headline ("Trump and Xi met in Beijing to discuss trade and Taiwan")
when a cluster grows large. With prompt caching it ran roughly $10–15/month
in earlier testing.

Your current `.env` has `SUMMARIZER_ENABLED=false`, and the ingest log
confirms every cycle skips the summarizer with the line:

```
worldview_api.cluster.summarize :: summarizer disabled via SUMMARIZER_ENABLED=false — skipping
```

So **current spend on Anthropic is $0**. The `ANTHROPIC_API_KEY` is still
present in `.env` because the code reads it at import time even when
disabled, but it never hits the API. If you ever want to turn the
summarizer back on, flip the flag to `true` and the existing
prompt-caching logic kicks in automatically.

**Recommendation for deploy:** carry `SUMMARIZER_ENABLED=false` over to
`.env.production` and leave it off for the first week so you can see the
no-LLM baseline. If cluster titles look weak (e.g. one outlet's clickbait
representing 50 sources), flip it on and watch the Anthropic dashboard
for a day before deciding.

---

## TL;DR recommendation

**Frontend → Cloudflare Pages (free).** Static SPA, unlimited bandwidth,
global CDN, GitHub auto-deploy on push. (Cloudflare is a US company,
HQ San Francisco.)

**Backend → DigitalOcean Droplet, $12/mo "Premium AMD" tier.** 1 vCPU,
2 GB RAM, 70 GB NVMe SSD, 3 TB egress. US datacenters (NYC, SFO, ATL,
others). Run Postgres + API + 15-min ingest all on one box via Docker
Compose, fronted by Caddy for automatic Let's Encrypt TLS.

**Total recurring: ~$12/mo. No external API costs right now** — see the
"Why is there an Anthropic key?" section below; the summarizer is disabled
and all embedding is done locally and for free with a CPU model.

If you want extra headroom for traffic spikes or want to skip the
DB-self-hosting tax, see "Hosting options compared" below — there's a
2 GB / 2 vCPU DigitalOcean tier at $18/mo and a managed-Postgres
variant (DO + Supabase) at $30/mo.

---

## Hosting options compared (US-based providers only)

All providers below are US companies with US datacenters. Hetzner and OVH
(European) intentionally excluded.

| Stack | Backend cost | DB cost | Total | Pros | Cons |
|---|---|---|---|---|---|
| **A. DigitalOcean Premium AMD Droplet, 2 GB + self-hosted Postgres (recommended)** | $12/mo | included on the box | **$12/mo** | US HQ (NY). Polished console, NVMe SSD, snapshots, simple flat pricing. Easy to bump to 4 GB if needed. | You patch the OS and back up the DB yourself. Single point of failure. |
| **B. Vultr Cloud Compute 2 GB + self-hosted Postgres** | $12/mo | included | **$12/mo** | US HQ (NJ). 16 US regions — pick whichever is closest. NVMe. | Smaller ecosystem than DO; the console is functional but less slick. |
| **C. AWS Lightsail 2 GB + self-hosted Postgres** | $12/mo | included | **$12/mo** | US HQ. Easy bridge into the rest of AWS later (S3 for backups, CloudFront, Route53). 3 TB egress. | The "Amazon-but-not-quite" sub-product can feel quirky; networking model is non-standard. |
| **D. DigitalOcean 2 GB Droplet + Supabase Pro (managed Postgres)** | $12/mo | $25/mo (8 GB, PostGIS + pgvector + HNSW, automatic PITR backups) | **$37/mo** | US-managed Postgres with backups, branching, dashboard. No DB ops on your end. | 3× the cost. Adds a network hop and a vendor. |
| **E. Render Web Service + Render Postgres** | $7/mo (Starter, always-on) + free cron | $7/mo (Basic 256 MB → upgrade as needed) | **$14–25/mo** | US-managed. Push-to-deploy. Cron built in. PostGIS + pgvector available. | Less control over the runtime. Postgres tiers above Basic step up fast. |
| **F. Fly.io machine (US region) + Neon Postgres (US)** | ~$5/mo (1 GB shared) | $0–19/mo | **$5–24/mo** | Both US-based. Fly has 13 US regions. Neon free tier 0.5 GB with PostGIS + pgvector. | Cron is fiddly on Fly (second machine or external pinger). 1 GB shared is tight. |
| **G. Railway** | usage-based | included | **~$15–25/mo** | US-based (San Francisco). Push-to-deploy, PostGIS + pgvector built in. | Usage billing surprises are the standard complaint. |

Why not Vercel / Netlify / Cloudflare Workers for the backend: they're
serverless. fastembed needs to load a 130 MB ONNX model into a long-lived
process; doing that on cold start adds seconds to every request. Also the
ingest job runs for ~30–90 s every 15 min — fine on a VPS, awkward on
serverless time/memory limits.

Why not Heroku: $7/mo Eco dyno sleeps after 30 min idle. Their Postgres
Mini is $5/mo for 10 M rows, no PostGIS without extra setup.

### Why 2 GB and not the $6/mo 1 GB tier?

Steady-state RAM on the live box:

```
Postgres 17 + PostGIS + pgvector (300 MB DB)   ≈ 200 MB
fastembed model loaded into memory             ≈ 250 MB
uvicorn + FastAPI                              ≈ 120 MB
ingest worker idle (between 15-min ticks)      ≈ 100 MB
Linux + Caddy + Docker overhead                ≈ 250 MB
                                              ─────────
                                                ~920 MB sustained
```

During the 15-min ingest tick, the embed+cluster step briefly pushes peak
RAM to ~1.3 GB. On a 1 GB droplet you'd OOM during ingestion. 2 GB gives
you ~700 MB of headroom for the spike and DB cache. You can downgrade
to 1 GB later if you split Postgres off to a managed service.

---

## If you specifically want AWS / Azure / GCP / Cloudflare

The table above leans toward "small, focused" US providers. If you'd rather
stay within a hyperscaler ecosystem (existing billing relationship, IAM,
audit log requirements, etc.), here are the equivalents.

All prices are 2026 US-region list prices, on-demand. Reserved/committed-use
discounts of 30–50% are available on most of these but not assumed here.

### AWS (us-east-1)

| Configuration | Monthly | Notes |
|---|---|---|
| **Lightsail 2 GB** (2 vCPU, 60 GB SSD, 3 TB egress) — Postgres + API + ingest on one instance | **$12** | Flat-rate, no surprise bills. The "cheap AWS" answer, functionally identical to the DO recommendation. |
| EC2 `t4g.small` (2 vCPU ARM, 2 GB) + 30 GB gp3 + S3/CloudFront for the SPA | $15–18 | More AWS-native but you wire it up yourself. |
| EC2 `t4g.small` + **RDS PostgreSQL** `db.t4g.micro` (1 vCPU, 1 GB) + 20 GB | $27–32 | Managed DB, automatic backups. PostGIS + pgvector both supported on RDS. |
| Aurora Serverless v2 (min 0.5 ACU always-on) | $43+ | Designed for spiky workloads at much larger scale. Overkill here. Skip. |
| ECS Fargate + RDS | $40+ | Same overkill comment. |

### Azure (East US)

| Configuration | Monthly | Notes |
|---|---|---|
| VM `B1ms` (1 vCPU, 2 GB) + 32 GB managed disk — Postgres + API self-hosted | $20 | Cheapest sensible Azure. |
| **App Service B1** (1 vCPU, 1.75 GB Linux) + **Postgres Flexible Server B1ms** (1 vCPU, 2 GB, 32 GB) + Static Web Apps (free tier) | $35 | Managed, push-to-deploy. PostGIS + pgvector both GA on Flex. |
| Container Apps min-replicas=1 + Pg Flex | $50+ | Pricier than App Service for steady workloads. |

Azure runs consistently ~50% higher than AWS or GCP at this scale.

### Google Cloud (us-east1)

| Configuration | Monthly | Notes |
|---|---|---|
| **`e2-small`** (2 shared vCPU, 2 GB) + 30 GB pd-balanced — self-hosted Postgres. Sustained-use discount applied automatically. | **$11–13** | Closest match to the DO recommendation in price and shape. |
| **Cloud Run** min-instances=1 (1 vCPU, 2 GB always-allocated) + **Cloud SQL `db-g1-small`** (1 vCPU, 1.7 GB, 10 GB) | $42 | Managed everywhere. pgvector GA on Cloud SQL late 2024. |
| Cloud Run scale-to-zero + Cloud SQL | $10–15 | Cheap on paper, but fastembed loads ~250 MB on cold start → 3–5 s first-request lag. Bad UX for the globe's "fly in and load" UX. |

### Cloudflare

The honest answer: **Cloudflare can host the *frontend* for free (Pages,
already in the plan), but it cannot host this backend without rewriting
it.**

| Why not |
|---|
| **Workers** (Cloudflare's compute) runs JS or Python-via-Pyodide. Loading a 130 MB ONNX `fastembed` model on every cold start is impractical, and Workers don't keep long-lived processes anyway. |
| **D1** (their SQLite-based DB) has no PostGIS, no pgvector. The geo + vector search that powers the globe wouldn't work. |
| **Vectorize** (their vector DB) could replace pgvector but loses the "join with Postgres" workflow — you'd need a second store for non-vector data, doubling complexity. |
| **Containers on Workers** (newer) is the closest fit but billed per second on `Standard-1` (0.5 vCPU, 4 GiB) at ~$0.0033/min always-on → roughly **$140/mo** if kept running. Designed for bursty workloads, not 24/7 services. |

**Realistic Cloudflare usage** for worldview: Cloudflare Pages for the
frontend (free, already in the plan), and *somebody else's* compute for
the backend. Trying to go Cloudflare-only would require a full rewrite
that removes PostGIS, splits the vector store from the metadata store,
and eats cold-start latency on every search.

### Side-by-side summary

| Where to host | Frontend | Backend | DB | Monthly | Setup effort |
|---|---|---|---|---|---|
| **DigitalOcean** (recommended) | Cloudflare Pages | $12 droplet | self-hosted on droplet | **$12** | low |
| AWS, cheap | Cloudflare Pages | Lightsail 2 GB | self-hosted on Lightsail | **$12** | low |
| AWS, managed | S3 + CloudFront (~$1) | EC2 t4g.small | RDS t4g.micro | **~$31** | medium |
| GCP, cheap | Cloudflare Pages | e2-small | self-hosted on e2-small | **~$12** | low |
| GCP, managed | Cloudflare Pages | Cloud Run min=1 | Cloud SQL g1-small | **~$42** | medium |
| Azure, cheap | Static Web Apps (free) | VM B1ms | self-hosted on VM | **~$20** | low |
| Azure, managed | Static Web Apps (free) | App Service B1 | Postgres Flex B1ms | **~$35** | medium |
| Cloudflare only | Pages (free) | not feasible | not feasible | n/a | — |

At this scale the cheapest options on DO, AWS Lightsail, and GCP e2-small
all land at ~$12/mo and are functionally interchangeable. Pick by
ecosystem familiarity. Azure costs ~50% more for equivalent compute.
Going fully managed on AWS or GCP triples the bill but eliminates the
DB-operations burden. Cloudflare wins for the frontend but isn't a
realistic backend host today.

---

## Recommended stack: deployment guide

This is the DigitalOcean-based plan. The Supabase / managed-Postgres
variant is a small detour noted inline.

### Architecture

```
                 cloudflare DNS
                       │
       ┌───────────────┴────────────────┐
       │                                │
       ▼                                ▼
   yourdomain.com               api.yourdomain.com
   (Cloudflare Pages)           (DigitalOcean Droplet)
                                    │
                                    ▼
                         ┌────────────────────────────┐
                         │  Caddy (TLS, reverse proxy)│
                         └──────────────┬─────────────┘
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              ▼                         ▼                         ▼
      ┌───────────────┐         ┌───────────────┐         ┌────────────────┐
      │  uvicorn/api  │         │   Postgres 17 │         │  ingest-cron   │
      │  (Docker)     │◄────────│  + PostGIS    │◄────────│ run_all.py /15m│
      │               │         │  + pgvector   │         │  (systemd timer│
      └───────────────┘         └───────────────┘         │   or cron)     │
                                                         └────────────────┘
```

### One-time setup

1. **Buy the droplet.** DigitalOcean → Create → Droplet → choose:
   - Image: Ubuntu 24.04 LTS
   - Size: **Premium AMD, 2 GB / 1 vCPU / 70 GB NVMe — $12/mo**
   - Region: NYC3, SFO3, or ATL1 (closest to expected viewers)
   - Auth: SSH key
   - Backups: optional (+20% of cost, $2.40/mo — recommended)

   Boots in ~45 seconds. Save the IPv4 address.

2. **DNS at Cloudflare.**
   - `A  yourdomain.com         → Pages` (Cloudflare adds this automatically
     when you connect Pages)
   - `A  api.yourdomain.com     → <Droplet IPv4>`
   - `AAAA api.yourdomain.com   → <Droplet IPv6>` (optional)
   - Proxy status for `api`: **DNS only (grey cloud)** initially so Caddy
     can issue Let's Encrypt certs without Cloudflare's edge in the way.
     You can flip on the orange cloud later if you want WAF/DDoS.

3. **Harden the box.**
   ```bash
   ssh root@<ip>
   adduser deploy && usermod -aG sudo deploy
   rsync --archive --chown=deploy:deploy ~/.ssh /home/deploy/
   # disable root + password login
   sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
   sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
   systemctl reload ssh
   ufw allow 22 && ufw allow 80 && ufw allow 443 && ufw --force enable
   apt update && apt -y upgrade && apt -y install unattended-upgrades
   ```

4. **Install Docker + Compose.**
   ```bash
   curl -fsSL https://get.docker.com | sh
   usermod -aG docker deploy
   ```

### Repository layout to add

Create these in `worldview-api/`:

```
worldview-api/
  docker/
    Dockerfile               # the FastAPI image
    Dockerfile.ingest        # same base, runs run_all.py
  compose.yaml               # services: db, api, ingest, caddy
  Caddyfile                  # TLS + reverse proxy
  .env.production            # gitignored; copied to /opt/worldview/.env
```

#### `docker/Dockerfile`

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml .
COPY src ./src
COPY scripts ./scripts
RUN pip install --no-cache-dir -e . fastembed anthropic
# Warm the fastembed model into the image so first request is instant
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"
EXPOSE 8088
CMD ["python", "scripts/serve.py"]
```

#### `compose.yaml`

```yaml
services:
  db:
    image: postgis/postgis:17-3.5
    restart: unless-stopped
    environment:
      POSTGRES_USER: worldview
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: worldview_prod
    volumes:
      - db-data:/var/lib/postgresql/data
      - ./sql:/docker-entrypoint-initdb.d:ro
    command: >
      postgres -c shared_preload_libraries=vector
               -c max_connections=50

  api:
    build: { context: ., dockerfile: docker/Dockerfile }
    restart: unless-stopped
    environment:
      DATABASE_URL: postgresql://worldview:${POSTGRES_PASSWORD}@db:5432/worldview_prod
      CORS_ORIGINS: https://yourdomain.com
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
    depends_on: [db]

  ingest:
    build: { context: ., dockerfile: docker/Dockerfile }
    restart: unless-stopped
    environment:
      DATABASE_URL: postgresql://worldview:${POSTGRES_PASSWORD}@db:5432/worldview_prod
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
    command: >
      sh -c "while true; do python scripts/run_all.py; sleep 900; done"
    depends_on: [db]

  caddy:
    image: caddy:2
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
      - caddy-config:/config
    depends_on: [api]

volumes:
  db-data:
  caddy-data:
  caddy-config:
```

> **Note on the ingest loop.** A simple `while true; sleep 900` in a
> long-lived container is fine for this cadence. If you ever want
> hourly+daily jobs at different cadences, replace with `supercronic`
> (a Docker-friendly cron) or a systemd timer on the host.

#### `Caddyfile`

```
api.yourdomain.com {
  encode zstd gzip
  reverse_proxy api:8088
  # rate-limit /search since it triggers embedding work
  @search path /search
  rate_limit @search 60r/m
}
```

(Skip the rate_limit block if you don't want to pull in the caddy
ratelimit plugin — for a personal demo it's overkill.)

#### `.env.production` (kept on the server only)

```
POSTGRES_PASSWORD=<32-char random>
ANTHROPIC_API_KEY=sk-ant-...
```

### First deploy

```bash
# from your laptop, push the repo somewhere the VPS can reach.
# easiest: a private github repo + a deploy key on the VPS.

ssh deploy@api.yourdomain.com
sudo mkdir -p /opt/worldview && sudo chown deploy:deploy /opt/worldview
cd /opt/worldview
git clone git@github.com:you/worldview-api.git .
cp /tmp/.env.production .env       # transferred via scp earlier
docker compose up -d --build
docker compose exec db psql -U worldview -d worldview_prod \
  -c "CREATE EXTENSION IF NOT EXISTS postgis;
      CREATE EXTENSION IF NOT EXISTS vector;
      CREATE EXTENSION IF NOT EXISTS pgcrypto;"
# Apply schema (treat sql/*.sql as ordered migrations):
for f in sql/*.sql; do
  docker compose exec -T db psql -U worldview -d worldview_prod < "$f"
done
```

Verify:

```bash
curl https://api.yourdomain.com/health     # → {"status":"ok"}
docker compose logs --tail=50 ingest       # see the first run
docker compose logs --tail=50 api
```

### Frontend deploy (Cloudflare Pages)

1. Push `~/worldview/` to its own GitHub repo.
2. Cloudflare dashboard → Pages → Connect to Git → pick the repo.
3. Build settings:
   - Framework preset: **Vite**
   - Build command: `npm run build`
   - Build output: `dist`
   - Environment variables: `VITE_API_BASE = https://api.yourdomain.com`
4. Custom domain: `yourdomain.com` and `www.yourdomain.com`.

Pages will build on every push to `main`. Preview deploys for every branch.

---

## Pre-deploy checklist (carry over from local dev)

These all came up in the readiness scan and need to be done before the
public domain works end-to-end.

- [ ] **Backend `.env.production`** has `CORS_ORIGINS=https://yourdomain.com`
      (and `https://www.yourdomain.com` if you use it). Default is localhost
      and will block the live frontend.
- [ ] **Frontend `VITE_API_BASE`** set in Cloudflare Pages env vars.
      Without it the build defaults to `http://127.0.0.1:8088` and the
      live site will look broken everywhere except your laptop.
- [ ] **Anthropic key** moved into server-side env only. Never bundled
      into the frontend (currently isn't — confirm with
      `grep -r ANTHROPIC dist/` after `npm run build`).
- [ ] **Confirm `.env` is gitignored** in `worldview-api`:
      `git check-ignore -v .env`. If it isn't, rotate the Anthropic key
      before pushing.
- [ ] **Add basic HTML meta** to `index.html`: description, og:title,
      og:image, theme-color. Currently it's a one-line `<title>worldview</title>`
      and shares look blank on Slack/Twitter.
- [ ] **Lint errors** (`npm run lint`): three small fixes in
      `useAppStore.ts:124,139` (ternary-as-statement) and `clouds.ts:23`
      (unused `_elapsed`). Build still passes but it's noisy.
- [ ] **Bundle size** is 820 KB / 222 KB gz. Above Vite's 500 KB warning
      but fine on a CDN. Optional: dynamic-import the markets panel
      and search results to drop initial paint.

---

## Operations after launch

### Backups

- Postgres on the droplet: a nightly `pg_dump` to a second location.
  ```bash
  # /etc/cron.daily/worldview-backup
  cd /opt/worldview
  docker compose exec -T db pg_dump -U worldview worldview_prod \
    | gzip > /opt/backups/worldview-$(date +%F).sql.gz
  # rotate: keep last 14
  ls -1t /opt/backups/worldview-*.sql.gz | tail -n +15 | xargs -r rm
  ```
  Push `/opt/backups` weekly to Cloudflare R2 (free 10 GB) or Backblaze B2.
- If you switch to Supabase: automatic daily PITR backups on Pro tier.

### Monitoring (lightweight)

- **Uptime:** [UptimeRobot](https://uptimerobot.com/) free tier, 5-min checks
  on `https://api.yourdomain.com/health` and `https://yourdomain.com`.
- **Logs:** `docker compose logs` is fine for week 1. If you want them
  persisted, point Caddy at a `journald` driver or ship to Better Stack
  (free 1 GB/mo).
- **Cost telemetry:** at launch there is **no variable-cost line** —
  Anthropic is disabled and embeddings are local. The only ongoing bill
  is the $12/mo droplet (+ $2.40/mo if you enabled DO backups). If you
  later flip `SUMMARIZER_ENABLED=true`, watch the Anthropic dashboard
  and set a $50/mo billing alert in the Anthropic console.

### Updates

```bash
ssh deploy@api.yourdomain.com
cd /opt/worldview
git pull
docker compose up -d --build
```

Zero-downtime not necessary at this scale; expect ~5 s of 502s during
the rebuild. If that bites, add a second `api` replica behind Caddy.

### Scaling triggers (not needed at launch)

- DB > 5 GB → move to Supabase Pro ($25/mo) or DO Managed Postgres ($15/mo),
  drop the local `db` service from compose.
- API > 30 req/s sustained → bump the droplet to the 4 GB / 2 vCPU tier
  ($24/mo), or split api and db onto separate boxes.
- Ingest taking > 5 min → split the embedding step into a worker queue
  (Redis + RQ) so it doesn't block the next 15-min tick.

---

## Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| DigitalOcean outage takes the whole API down | Low (DO publishes 99.99% droplet SLA) | The frontend on Cloudflare Pages keeps loading; users see a "feed temporarily unavailable" instead of a blank page. Add a friendly fallback in `Globe.tsx` when `apiHealth()` returns false. |
| Anthropic spend spikes | Currently $0 (summarizer disabled) | If you re-enable: importance threshold + `SUMMARIZER_ENABLED` flag let you cut spend back to $0 instantly. Set a $50/mo billing alert in the Anthropic console before flipping on. |
| GDELT changes their CSV format | Low | Workers raise on schema mismatch; ingest logs go to `docker compose logs ingest`. Worst case: stop the ingest service, keep serving cached clusters. |
| pg_dump backup never tested | High | After deploy, do a dry-run restore into a throwaway local DB. A backup you've never restored is not a backup. |
| Public URL gets scraped | Medium | Caddy rate-limit on `/search` already covers the expensive endpoint. Add `Cache-Control: public, max-age=30` to `/clusters` for cheap relief. |

---

## Decision points for you

1. **Self-hosted Postgres on the droplet ($12/mo total) vs managed
   (DO + Supabase, $37/mo)?** Cheaper-and-DIY vs more-expensive-and-managed.
2. **DigitalOcean region?** NYC3, SFO3, ATL1, TOR1 are the US/Canada
   options. Pick whichever is closest to where you and your viewers are —
   NYC3 is the typical default for east-coast US.
3. **Repo strategy?** Both projects in one monorepo, or stay split? Split
   is fine — Cloudflare Pages doesn't care.
4. **Subdomain naming?** `api.yourdomain.com` is the obvious pick, but
   `globe.yourdomain.com` + `globe-api.yourdomain.com` also works if you
   want the apex for a portfolio site.
5. **Anthropic summarizer — leave off, or flip on after launch?** Off
   ships free; on costs ~$10–15/mo and gives nicer AP-style cluster
   headlines. The current build runs perfectly fine without it.

Tell me which way to go on (1) and (2) and I'll start scaffolding the
Dockerfile + compose.yaml + Caddyfile in the repo.
