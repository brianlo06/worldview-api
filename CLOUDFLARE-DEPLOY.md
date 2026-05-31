# Worldview / jarvisworlds.com — Cloudflare deployment guide

End-to-end deploy walkthrough for **jarvisworlds.com** using Cloudflare
for everything we can, and Supabase for the one thing we can't (Postgres
with both PostGIS + pgvector — Cloudflare doesn't host Postgres).

This supersedes the DigitalOcean-droplet plan in `DEPLOY.md`. The
`docker/Dockerfile` we built for Phase 4 carries over unchanged.
`compose.yaml` / `Caddyfile` are *not* used in this deployment (they
remain useful for local dev if you ever want to spin the full stack on
your laptop).

---

## Final architecture

```
                            Cloudflare
                 ┌──────────────────────────────────┐
   browser  ───► │  edge (TLS, DDoS, gzip, WAF)     │
                 └────────┬─────────────────┬───────┘
                          │                 │
              jarvisworlds.com   api.jarvisworlds.com
                          │                 │
                          ▼                 ▼
                  ┌──────────────┐   ┌────────────────────┐
                  │   Pages      │   │   Containers       │
                  │  (frontend)  │   │  (FastAPI image)   │
                  │  static SPA  │   │  + ingest variant  │
                  └──────────────┘   └─────────┬──────────┘
                                               │
                                               ▼
                                     ┌──────────────────┐
                                     │  Supabase        │
                                     │  Postgres 17     │
                                     │  + PostGIS       │
                                     │  + pgvector      │
                                     └──────────────────┘
```

What lives where:

| Piece | Where | Why |
|---|---|---|
| Domain registrar | Cloudflare Registrar | At-cost pricing, no renewal hike. |
| DNS | Cloudflare | Already required by Pages + ACME. |
| Frontend | Cloudflare Pages | Free, generous limits, native fit with the existing Vite build. |
| Backend API | Cloudflare Containers | GA 2026-04-13; runs the unmodified `docker/Dockerfile`. |
| Ingest job | Cloudflare Containers | Same image, different command (`run_all.py` loop). |
| TLS + rate-limit | Cloudflare edge | Replaces Caddy entirely. |
| Postgres | **Supabase** | Only managed-PG with **both** PostGIS + pgvector on the free tier. |

---

## Cost — at a glance

Three honest tiers. The "cheap path" is the answer if you want minimum
spend; tier 2 is what most public sites should actually run.

### Tier 1 — Cheap path (~$70/yr)

| Item | Cost |
|---|---|
| `jarvisworlds.com` at Cloudflare Registrar | ~$10.44/yr |
| Cloudflare Pages (frontend) | $0 (free tier) |
| Cloudflare DNS | $0 |
| Cloudflare Workers Paid plan (required for Containers) | $5/mo = $60/yr |
| Container compute (low traffic — within the Paid-plan allowance) | ~$0/mo expected |
| Supabase free tier (500 MB DB, 5 GB egress, both extensions) | $0 |
| **Total** | **~$70/yr** |

**The catch:** Supabase free-tier projects **pause after 1 week of
inactivity**. First request after a pause has a several-second cold
start, and if the project stays paused long enough the DB needs a
manual unpause. For a personal demo this is fine; for anything
public-facing you usually want to either (a) ping it on a schedule to
keep it warm, or (b) move to Tier 2.

### Tier 2 — Cheap and reliable (~$370/yr)

Same stack, but Supabase Pro removes the auto-pause and bumps limits.

| Item | Cost |
|---|---|
| Domain | ~$10/yr |
| Cloudflare Workers Paid (Containers) | $60/yr |
| Supabase Pro | $25/mo = $300/yr |
| **Total** | **~$370/yr** |

### Tier 3 — Original droplet plan (~$185/yr)

For reference: if you reverted to the original DigitalOcean plan in
`DEPLOY.md`, the math is domain + $12/mo droplet + $2.40/mo backups =
~$184.80/yr — actually *cheaper than Tier 2* because you self-host
Postgres and don't pay Supabase. The tradeoffs: you babysit a Linux
box, run `docker compose` and `apt upgrade` yourself, and don't get
Cloudflare's edge in front of the API. For a personal project the
droplet plan is genuinely competitive; for something you want to scale
without ops, Tier 2 wins.

---

## Prerequisites

- [x] `~/worldview` and `~/worldview-api` are on private GitHub
      (`brianlo06/worldview`, `brianlo06/worldview-api`) at current
      `main`. Done.
- [x] `docker/Dockerfile` exists and pre-warms the embedding model.
      Done (Phase 4).
- [ ] You have a Cloudflare account.
- [ ] You have a Supabase account.
- [ ] `wrangler` CLI installed (`npm i -g wrangler`); `npx wrangler login`
      once on first use.

---

## Step 1 — Register `jarvisworlds.com` at Cloudflare

1. Cloudflare dashboard → **Registrar** → **Register domains**.
2. Search `jarvisworlds` → pick `.com` → checkout. ~$10.44 charged for
   the first year; renewal stays at the same at-cost price.
3. DNS for the domain is created automatically and zone is active
   within a minute or two.
4. (No nameserver change to make — Cloudflare-registered domains use
   Cloudflare nameservers by default.)

---

## Step 2 — Stand up the database at Supabase

1. supabase.com → **New project**:
   - Name: `jarvisworlds-prod`
   - DB password: generate + save. You'll need it for the connection string.
   - Region: pick whatever matches your CF Container region (typically
     the closest one — `us-east-1` is a fine default).
   - Plan: **Free** (Tier 1) or **Pro** (Tier 2). Defer the upgrade
     decision; you can switch later without losing data.
2. Once the project is up: **Database → Extensions**, enable:
   - `postgis`
   - `vector`
   - `pgcrypto` (already enabled by default usually; confirm)
3. Apply the worldview schema. From your laptop:
   ```bash
   # Get the "Connection string (URI)" from Supabase → Project Settings
   # → Database → Connection string. Use the *direct* connection (not
   # the pooler) for one-off schema work.
   export SUPA_URL='postgresql://postgres.<ref>:<password>@db.<ref>.supabase.co:5432/postgres'
   cd ~/worldview-api
   for f in sql/*.sql; do
     echo ">> $f"
     psql "$SUPA_URL" -v ON_ERROR_STOP=1 -f "$f"
   done
   ```
4. Sanity check:
   ```bash
   psql "$SUPA_URL" -c "SELECT extname FROM pg_extension ORDER BY extname;"
   # → expect: pgcrypto, plpgsql, postgis, vector
   psql "$SUPA_URL" -c "\dt"
   # → expect: events, clusters, anomalies, markets, etc.
   ```
5. For the runtime, grab the **pooled** connection string instead
   (Project Settings → Database → Connection pooling, Transaction mode,
   port 6543). That's what Cloudflare Containers will use — Supabase's
   pooler handles short-lived connections from edge runtimes cleanly:
   ```
   DATABASE_URL=postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres
   ```

---

## Step 3 — Deploy the API to Cloudflare Containers

The image is already built in `docker/Dockerfile`. Containers needs a
`wrangler.toml` to register the deployment.

1. **Pre-flight: install wrangler + log in.**
   ```bash
   cd ~/worldview-api
   npm init -y      # only if there's no package.json yet
   npm i -D wrangler@latest
   npx wrangler login
   ```

2. **Create `wrangler.toml`** at the repo root:
   ```toml
   name = "jarvisworlds-api"
   main = "src/worker.js"          # see step 3 below
   compatibility_date = "2026-05-01"

   # The container image. wrangler builds from this Dockerfile on each deploy.
   [[containers]]
   name = "api"
   image = "./docker/Dockerfile"
   max_instances = 1               # raise as traffic grows
   instance_type = "basic"         # 256 MB / 1/16 vCPU baseline
   # rolling_updates: true (default) — zero-downtime deploys
   ```

3. **Create the tiny Worker entrypoint** at `src/worker.js`. Workers
   front Containers in CF's model — the Worker just forwards every
   request to the container:
   ```js
   import { Container, getContainer } from "@cloudflare/containers";

   export class Api extends Container {
     defaultPort = 8088;
     sleepAfter = "10m";    // scale to zero after 10m idle (saves $)
   }

   export default {
     async fetch(req, env) {
       const c = getContainer(env.API);
       return c.fetch(req);
     },
   };
   ```
   And add the binding to `wrangler.toml`:
   ```toml
   [[durable_objects.bindings]]
   name = "API"
   class_name = "Api"

   [[migrations]]
   tag = "v1"
   new_sqlite_classes = ["Api"]
   ```
   (Containers bindings ride on Durable Objects under the hood — this
   is the standard pattern.)

4. **Inject secrets** (not in `wrangler.toml`):
   ```bash
   npx wrangler secret put DATABASE_URL          # paste the Supabase pooled URI
   npx wrangler secret put CORS_ORIGINS          # https://jarvisworlds.com,https://www.jarvisworlds.com
   npx wrangler secret put GDELT_USER_AGENT      # jarvisworlds-prod/1.0 (+https://jarvisworlds.com)
   npx wrangler secret put SUMMARIZER_ENABLED    # false
   # Only set this if/when you actually flip the summarizer on:
   # npx wrangler secret put ANTHROPIC_API_KEY
   ```

5. **Deploy.**
   ```bash
   npx wrangler deploy
   ```
   First deploy will build the Docker image, push to CF's registry, and
   start the container. Takes a few minutes (image build + fastembed
   model warm).

6. **Test directly** (before DNS is wired):
   ```bash
   curl https://jarvisworlds-api.<your-cf-subdomain>.workers.dev/health
   # → {"status":"ok","db":"ok"}   (the real DB check we built earlier)
   ```

---

## Step 4 — Deploy the ingest job

Same image, different command. Two patterns — pick one:

**Pattern A (simplest, mirrors the existing `compose.yaml` loop).**
Add a second container in `wrangler.toml`:

```toml
[[containers]]
name = "ingest"
image = "./docker/Dockerfile"
max_instances = 1
instance_type = "basic"
command = ["sh", "-c", "while true; do python scripts/run_all.py || true; sleep 900; done"]
```

The persistent loop pattern bills you for idle memory (~$1/mo for
512 MB) plus actual CPU during the ~30s/cycle ingest run.

**Pattern B (more cloud-native, lower cost).** Use a Cloudflare Cron
Trigger to invoke `run_all.py` every 15 minutes:

```toml
[triggers]
crons = ["*/15 * * * *"]
```

…and have the Worker, when invoked by a cron event, run a one-shot
container that exits when `run_all.py` completes. Pays only for actual
CPU during the run. More moving parts; defer until Pattern A's cost is
proven worth optimizing.

The ingest container needs the same secrets as the API (`DATABASE_URL`,
`GDELT_USER_AGENT`, optionally `ANTHROPIC_API_KEY`). Secrets are scoped
per Worker, so they're already shared if you use one `wrangler.toml`.

---

## Step 5 — Deploy the frontend to Cloudflare Pages

1. Cloudflare dashboard → **Workers & Pages** → **Create** → **Pages**
   → **Connect to Git**.
2. Pick `brianlo06/worldview` (private — Cloudflare needs the GitHub
   app connection, which it will prompt for once).
3. Build settings:
   - **Framework preset:** Vite
   - **Build command:** `npm run build`
   - **Build output directory:** `dist`
   - **Production branch:** `main`
4. Environment variables (Production):
   - `VITE_API_BASE` = `https://api.jarvisworlds.com`
5. Click **Save and Deploy**. First build takes ~2 minutes.
6. Cloudflare gives you a `*.pages.dev` URL. Confirm it loads (will
   show "FEED OFFLINE · DEMO" until DNS for the API custom domain is
   wired in step 6).

---

## Step 6 — Wire the custom domains

1. **Frontend domain** (`jarvisworlds.com`).
   - Pages → your project → **Custom domains** → **Set up a custom
     domain** → enter `jarvisworlds.com`. Cloudflare creates the CNAME
     for you (since the zone is already on Cloudflare).
   - Repeat for `www.jarvisworlds.com` (add a redirect rule in
     Cloudflare to canonicalize www → apex if you want).
2. **API domain** (`api.jarvisworlds.com`).
   - Workers & Pages → `jarvisworlds-api` → **Settings → Triggers →
     Custom Domains** → add `api.jarvisworlds.com`. Cloudflare wires
     the route + cert automatically.
3. Cert provisioning takes ≤2 minutes. Verify:
   ```bash
   curl -sI https://jarvisworlds.com | head -3
   curl -s https://api.jarvisworlds.com/health
   # → {"status":"ok","db":"ok"}
   ```

---

## Step 7 — First-traffic smoke tests

```bash
# DB reachable from the deployed API
curl -s https://api.jarvisworlds.com/health
# → {"status":"ok","db":"ok"}

# A real data endpoint (returns empty array if ingest hasn't run yet)
curl -s 'https://api.jarvisworlds.com/clusters?limit=1' | head -c 200

# Frontend loads + connects
open https://jarvisworlds.com
#   → boot screen → globe appears → top-right HUD shows "● LIVE"
#   → no "FEED OFFLINE" banner

# Ingest is running
npx wrangler tail jarvisworlds-api --format=pretty
#   → expect "run_all.py" output every ~15 min
```

If the offline banner shows: `CORS_ORIGINS` secret is wrong or the API
custom domain isn't fully provisioned yet. If `/health` returns
`{"db":"unreachable"}`: the Supabase `DATABASE_URL` secret is wrong or
the project is paused (free tier).

---

## Edge protection (the bits Caddy used to do)

Cloudflare's edge handles all three of these for free now that you're
on its platform:

1. **TLS** — automatic Let's Encrypt-equivalent per domain. No config.
2. **gzip/brotli compression** — on by default.
3. **Rate-limit `/search`** — Dashboard → Security → WAF → **Rate
   Limiting Rules** → create:
   - Rule name: `search-throttle`
   - When incoming requests match: `URI Path equals /search` AND
     `Hostname equals api.jarvisworlds.com`
   - Then: **block** for 1 minute when traffic exceeds **30 requests
     per IP per minute**.

This replaces the `caddy-ratelimit` block in the original `Caddyfile`.
Same protection, fewer moving parts.

---

## Updates / re-deploys

| Change | Command |
|---|---|
| Frontend code | `git push` → Pages auto-builds + deploys |
| API code | `git push` then `cd ~/worldview-api && npx wrangler deploy` |
| API secret | `npx wrangler secret put NAME` (re-deploy not required) |
| DB schema migration | `psql "$SUPA_URL" -f sql/00X_new.sql` from laptop |
| Rotate Supabase password | Supabase dashboard → DB → reset → `wrangler secret put DATABASE_URL` |

---

## Known gotchas

- **Supabase free-tier auto-pause.** Project pauses after 7 days of no
  database activity. To avoid: ping `/health` on a schedule (a free
  Cloudflare Cron Trigger calling `https://api.jarvisworlds.com/health`
  every 6 days keeps it warm), OR upgrade to Supabase Pro.
- **Embedding model cold start.** The `Dockerfile` pre-downloads
  `bge-small-en-v1.5` at build time so first request after a Container
  spin-up is fast. If you ever swap the model, repeat the warm step.
- **Container `sleepAfter`.** Tuned to 10m above — that's a tradeoff
  between cost (longer = pays for idle memory) and latency (shorter =
  cold-start a few seconds after idle traffic). For a demo, 10m is the
  sweet spot.
- **CORS_ORIGINS gotcha.** If you serve `www.jarvisworlds.com` AND
  `jarvisworlds.com` from Pages, BOTH must be in `CORS_ORIGINS` or one
  of them will be blocked.
- **No more `docker compose up`.** `compose.yaml` and `Caddyfile` in
  this repo are now unused for production. Keep them for local
  full-stack dev or delete them; either is fine.

---

## Day-one checklist

- [ ] `jarvisworlds.com` registered at Cloudflare
- [ ] Supabase project created, password saved, postgis + vector enabled
- [ ] `sql/*.sql` applied to Supabase, extensions verified present
- [ ] `wrangler.toml` + `src/worker.js` committed
- [ ] All four secrets set (`DATABASE_URL`, `CORS_ORIGINS`, `GDELT_USER_AGENT`, `SUMMARIZER_ENABLED`)
- [ ] `npx wrangler deploy` succeeded; `*.workers.dev/health` returns ok
- [ ] Pages connected to `brianlo06/worldview`, first build green
- [ ] `VITE_API_BASE` set in Pages env vars
- [ ] Custom domains attached: `jarvisworlds.com` → Pages, `api.jarvisworlds.com` → Workers
- [ ] Rate-limit WAF rule on `/search` configured
- [ ] Smoke tests pass end-to-end (`curl /health`, `curl /clusters`, browser load)
- [ ] (Optional) Cron-trigger keep-warm hitting Supabase if on free tier
