# Worldview / jarvisworlds.com — Oracle Cloud Always Free deployment

End-to-end walkthrough for deploying on **Oracle Cloud Infrastructure (OCI)
Always Free**: 4 OCPUs / 24 GB RAM of ARM Ampere capacity, permanently
free. Frontend stays on Cloudflare Pages (Oracle doesn't compete on
static hosting); DNS stays at Cloudflare.

The Phase 4 stack (`docker/Dockerfile`, `compose.yaml`, `Caddyfile`,
`docker/Dockerfile.db`, `docker/Dockerfile.caddy`) carries over **as-is**
— Oracle Always Free is essentially "a free Linux droplet, but ARM",
which is exactly what `compose.yaml` was designed for. The single change
is the Docker images need to build for `linux/arm64`.

---

## Final architecture

```
                          Cloudflare DNS
                                │
              ┌─────────────────┴──────────────────┐
              │                                    │
              ▼                                    ▼
       jarvisworlds.com                  api.jarvisworlds.com
        (CF Pages, free)                      A → <VM IP>
                                                  │
                                                  ▼
                              ┌─────────────────────────────────┐
                              │  Oracle Always Free A1.Flex VM  │
                              │  4 OCPU ARM · 24 GB · 100 GB    │
                              │                                 │
                              │   docker compose:               │
                              │   ┌─────────┐  ┌─────────────┐  │
                              │   │ caddy   │──│ api  :8088  │  │
                              │   │ :80/443 │  ├─────────────┤  │
                              │   │   TLS   │  │ ingest loop │  │
                              │   │ ratelmt │  ├─────────────┤  │
                              │   └─────────┘  │ db (PG17+   │  │
                              │                │ PostGIS+    │  │
                              │                │ pgvector)   │  │
                              │                └─────────────┘  │
                              └─────────────────────────────────┘
```

What lives where:

| Piece | Where | Why |
|---|---|---|
| Domain | Cloudflare Registrar | At-cost pricing. |
| DNS | Cloudflare | Pages requires it; clean apex/api split. |
| Frontend | Cloudflare Pages | Free, great fit with Vite build. |
| Backend API + ingest + DB + TLS | One OCI A1.Flex VM | All four compose services on a single ARM box. |
| Edge (TLS, rate-limit, gzip) | Caddy in compose | Already configured in Phase 4. |

---

## Cost

| Item | Cost |
|---|---|
| `jarvisworlds.com` at Cloudflare Registrar | ~$10.44/yr |
| Cloudflare Pages (frontend) | $0 |
| Cloudflare DNS | $0 |
| Oracle A1.Flex VM (4 OCPU / 24 GB ARM) | $0 permanently |
| Oracle 100 GB boot volume | $0 |
| Oracle 10 TB/mo egress | $0 |
| **Total** | **~$10/yr** |

The only practical constraints are (1) "Out of Host Capacity" for A1.Flex
in popular regions, addressed at signup below, and (2) the reclamation
policy (idle CPU < 20% for 7 days), which doesn't apply to this workload
because the ingest job spikes CPU every 15 min.

---

## Prerequisites

- [x] `~/worldview` and `~/worldview-api` pushed to private GitHub.
- [x] `docker/Dockerfile` (api), `Dockerfile.db`, `Dockerfile.caddy`,
      `compose.yaml`, `Caddyfile` in `worldview-api/`. Phase 4 done.
- [ ] An OCI account. Signing up requires a credit card (Oracle won't
      charge unless you upgrade), a valid phone number, and patience —
      account approval can take ~15 min to a few hours.
- [ ] A Cloudflare account (for Pages + DNS + Registrar).

---

## Step 1 — Sign up for Oracle Cloud (pick your region carefully)

**The region you choose at signup is permanent for that account.**
A1.Flex capacity availability varies massively by region; if you pick
a popular US region you may not be able to provision an A1 instance
for days. Recommended priority order:

1. **us-phoenix-1** — best US capacity for A1 historically.
2. **us-sanjose-1** — second-best US for A1.
3. **uk-london-1**, **eu-frankfurt-1** — good capacity, fine if you're
   not US-centric.
4. **Avoid** us-ashburn-1 and us-chicago-1 unless you know A1 is open
   there — they're the most popular and most often "out of capacity."

At [signup](https://signup.cloud.oracle.com):
1. Email + name + country.
2. Verification SMS.
3. Credit card (for identity verification — no charge unless you
   manually upgrade to Pay As You Go).
4. **Home Region: pick from the list above.** This is the locked
   choice.
5. Account activation can take 15 min – 2 hrs. You'll get an email.

---

## Step 2 — Provision the A1.Flex VM

After login → Compute → Instances → **Create instance**.

| Field | Value |
|---|---|
| Name | `jarvisworlds` |
| **Image** | Canonical Ubuntu 24.04 (Always Free eligible) |
| **Shape** | **VM.Standard.A1.Flex** — set OCPUs=**4**, Memory=**24 GB** |
| Subnet | Default public subnet in your VCN (created automatically) |
| Assign public IPv4 | **Yes** |
| **SSH keys** | Upload `~/.ssh/id_ed25519.pub` (or paste your existing public key) |
| **Boot volume** | 100 GB (default is 47 GB; bump to 100 — Always Free includes 200 GB block total) |

Click **Create**. The instance shows "PROVISIONING" then "RUNNING" in
1–3 min — note the **public IP** that appears.

### If you hit "Out of Host Capacity"

Common in popular regions. Options, in increasing order of effort:

1. Click **Create** again immediately — capacity floats in and out.
2. Try a different Availability Domain in the same region (use the AD
   dropdown). Capacity is per-AD.
3. Try smaller — 2 OCPU / 12 GB. Some Always Free capacity is
   fragmented; smaller shapes succeed when 4-OCPU fails.
4. Schedule retries via a small shell loop (or use the
   "[oci-always-free-creator](https://github.com/hitrov/oci-arm-host-capacity)"
   community tool — runs in your own infra, retries on a schedule
   until it succeeds).

---

## Step 3 — Open ports in the VCN security list

Out of the box, the VCN's default security list allows only port 22.
Caddy needs 80 (ACME HTTP-01) and 443 (HTTPS).

Networking → Virtual Cloud Networks → your VCN → Subnet → Default
Security List → **Add Ingress Rules**:

| Source | Protocol | Port | Description |
|---|---|---|---|
| `0.0.0.0/0` | TCP | 80 | HTTP for Let's Encrypt + redirects |
| `0.0.0.0/0` | TCP | 443 | HTTPS API traffic |

**Do not** add port 8088 (the FastAPI port). The API is internal to the
compose network; only Caddy talks to it. Same for 5432 (Postgres) —
internal only.

After adding the rules, also enable them at the OS level (Oracle's
Ubuntu image ships with `iptables` rules that block them by default):

```bash
ssh ubuntu@<public-ip>
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

---

## Step 4 — Harden the box + install Docker

SSH in (key from step 2 should work):

```bash
ssh ubuntu@<public-ip>

# Updates + base tools
sudo apt update && sudo apt -y upgrade
sudo apt -y install ca-certificates curl gnupg git ufw fail2ban unattended-upgrades

# Disable password auth (key-only)
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl reload ssh

# Docker (official installer)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
# Log out + back in for the group change to take effect, OR:
newgrp docker
docker --version          # sanity
docker compose version    # sanity
```

---

## Step 5 — Pull repo + configure `.env`

```bash
# Create a deploy area
sudo mkdir -p /opt/worldview-api && sudo chown ubuntu:ubuntu /opt/worldview-api
cd /opt/worldview-api

# Clone the private repo. Use a GitHub deploy key (read-only on this repo).
# On your laptop:
#   ssh-keygen -t ed25519 -f ~/.ssh/wv_api_deploy -C "oracle vm deploy key"
#   then add ~/.ssh/wv_api_deploy.pub to GitHub repo:
#     brianlo06/worldview-api → Settings → Deploy keys → Add deploy key (read-only)
#   scp ~/.ssh/wv_api_deploy ubuntu@<ip>:~/.ssh/id_ed25519_wv
#
# Then on the VM:
cat >> ~/.ssh/config <<'EOF'
Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_ed25519_wv
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/id_ed25519_wv ~/.ssh/config
git clone git@github.com:brianlo06/worldview-api.git .

# Fill in .env from the template
cp .env.production.example .env
nano .env   # set the values below
```

Required `.env` values for compose (matches `.env.production.example`):

```ini
DOMAIN=jarvisworlds.com
POSTGRES_PASSWORD=<generate: openssl rand -base64 24>
POSTGRES_DB=worldview_prod
DATABASE_URL=postgresql://worldview:<the password above>@db:5432/worldview_prod
CORS_ORIGINS=https://jarvisworlds.com,https://www.jarvisworlds.com
GDELT_USER_AGENT=jarvisworlds-prod/1.0 (+https://jarvisworlds.com)
ANTHROPIC_API_KEY=
CLAUDE_SUMMARIZER_MODEL=claude-haiku-4-5
SUMMARIZER_ENABLED=false
```

Lock the file down:

```bash
chmod 600 .env
```

---

## Step 6 — Bring up the stack

Because A1 is ARM, Docker builds the images natively on the VM (no
emulation needed):

```bash
cd /opt/worldview-api
docker compose up -d --build
```

First build takes 8–15 min — most of that is downloading the fastembed
model and base images. Subsequent rebuilds are fast.

Watch the boot:

```bash
docker compose logs -f
# Ctrl-C when you see "Uvicorn running on http://0.0.0.0:8088"
# and the ingest container is past its first "writing N events" line.
```

Smoke tests **from the VM itself** (before DNS is wired):

```bash
# Health via Caddy (uses the in-compose hostname routing)
curl -k https://localhost/health
# → {"status":"ok","db":"ok"}

# Direct to api container, bypassing Caddy
docker compose exec api curl -s http://127.0.0.1:8088/health

# DB extensions installed?
docker compose exec db psql -U worldview -d worldview_prod \
  -c "SELECT extname FROM pg_extension ORDER BY extname;"
# → expect: pgcrypto, plpgsql, postgis, vector
```

---

## Step 7 — Deploy the frontend to Cloudflare Pages

(Identical to CLOUDFLARE-DEPLOY.md Step 5 — repeating here so this
guide stands alone.)

1. Cloudflare dashboard → **Workers & Pages** → **Create** → **Pages**
   → **Connect to Git** → select `brianlo06/worldview`.
2. Build settings:
   - Framework preset: **Vite**
   - Build command: `npm run build`
   - Build output: `dist`
   - Production branch: `main`
3. Environment variables (Production):
   - `VITE_API_BASE` = `https://api.jarvisworlds.com`
4. **Save and Deploy**.

---

## Step 8 — Wire DNS at Cloudflare

In Cloudflare dashboard → `jarvisworlds.com` zone → **DNS**:

| Type | Name | Content | Proxy |
|---|---|---|---|
| CNAME | `jarvisworlds.com` | `<your project>.pages.dev` | Proxied (orange cloud) |
| CNAME | `www` | `<your project>.pages.dev` | Proxied (orange cloud) |
| A | `api` | `<your VM public IP>` | **DNS only (grey cloud)** ← critical for ACME |

Why `api` must be grey-cloud initially: Caddy issues the Let's Encrypt
cert via HTTP-01, which requires the apex IP to be your VM (not
Cloudflare's edge). Once the cert is issued and stable, you *can* flip
on the orange cloud for DDoS protection — Caddy renewals will then
need to be re-done via DNS-01 (more setup; skip unless you actually
get attacked).

Also in Cloudflare → your Pages project → **Custom domains** → add
`jarvisworlds.com` and `www.jarvisworlds.com`. CF wires the cert.

Cert provisioning for `api.jarvisworlds.com` happens on the VM
automatically the first time anyone hits `https://api.jarvisworlds.com`:

```bash
# From your laptop, NOT the VM
curl https://api.jarvisworlds.com/health
# First request takes a few seconds while Caddy obtains the cert
# → {"status":"ok","db":"ok"}
```

---

## Step 9 — Full-stack smoke test

```bash
# Backend reachable, DB healthy
curl -s https://api.jarvisworlds.com/health
# → {"status":"ok","db":"ok"}

# A real data endpoint (empty array until ingest has run)
curl -s 'https://api.jarvisworlds.com/clusters?limit=1' | head -c 200

# Edge rate-limit working (built into the Caddyfile we deployed)
for i in $(seq 1 35); do
  curl -s -o /dev/null -w "%{http_code} " \
    -X POST https://api.jarvisworlds.com/search \
    -H 'Content-Type: application/json' \
    -d '{"query":"test"}'
done
# → first 30 = 200, then 429 (rate-limited)

# Frontend loads and connects to backend
open https://jarvisworlds.com
# → boot screen → globe → top-right HUD shows "● LIVE"
# → no "FEED OFFLINE" banner

# Ingest is running
docker compose logs --tail=50 ingest
```

---

## Maintenance

### Account activity (prevents account-level reclamation)

Oracle has been known to reclaim Always Free *accounts* (not just
idle VMs) whose owners haven't logged into the console in months. Log
into [cloud.oracle.com](https://cloud.oracle.com/) at least once every
~30 days. Calendar reminder recommended.

### CPU reclamation policy (not a concern for this workload)

OCI reclaims A1 instances whose 95th-percentile CPU is < 20% over any
7-day window. For Worldview this isn't a real risk — the ingest job
runs every 15 min and the API serves frontend polling continuously.
If you ever pause both for over a week, run a `stress -c 1 -t 60`
periodically to be safe.

### OS updates

Unattended-upgrades is enabled (step 4); the box auto-applies security
patches. Reboot monthly:

```bash
ssh ubuntu@<public-ip> 'sudo reboot'
```

After a reboot, compose restarts automatically because every service
has `restart: unless-stopped`.

### Postgres backups

The most important thing this guide doesn't auto-handle. The simplest
viable backup is a cron-scheduled `pg_dump` to OCI Object Storage
(which has 20 GB free):

```bash
# On the VM, set up an OCI Object Storage bucket via the console:
#   Storage → Buckets → Create → "worldview-backups"
#
# Then install rclone and a small backup script:
sudo apt -y install rclone
# Configure rclone for OCI Object Storage (S3-compatible):
#   rclone config  →  "New remote", type "s3", provider "Other"
#   Endpoint:  https://<namespace>.compat.objectstorage.<region>.oraclecloud.com
#   Access key + secret: generate from OCI → Identity → Users → Customer Secret Keys
```

Create `/opt/worldview-api/backup.sh`:

```bash
#!/bin/bash
set -euo pipefail
cd /opt/worldview-api
ts=$(date -u +%Y%m%dT%H%M%SZ)
docker compose exec -T db \
  pg_dump -U worldview -d worldview_prod -F c \
  > /tmp/worldview-${ts}.dump
rclone copy /tmp/worldview-${ts}.dump oci:worldview-backups/
rm /tmp/worldview-${ts}.dump
# Keep last 14 days
rclone delete oci:worldview-backups/ --min-age 14d
```

```bash
chmod +x backup.sh
# Daily at 04:00 UTC
echo '0 4 * * * /opt/worldview-api/backup.sh >> /var/log/wv-backup.log 2>&1' \
  | sudo tee /etc/cron.d/worldview-backup
```

### Updates / re-deploys

| Change | Command |
|---|---|
| Frontend code | `git push` → Pages auto-builds |
| API code | `ssh` to VM → `cd /opt/worldview-api && git pull && docker compose up -d --build` |
| `.env` change | edit `.env` → `docker compose up -d` (re-creates affected containers) |
| DB schema migration | `git pull` then `docker compose exec -T db psql -U worldview -d worldview_prod < sql/00X_new.sql` |
| Rotate POSTGRES_PASSWORD | edit `.env` → `docker compose down` → re-create db volume OR alter user in psql + restart |
| Rotate VM SSH keys | OCI console → instance → Console connection → add/rotate public keys |

### Monitoring

For a personal site you probably don't need this on day one, but it's
trivial to add later:

- **Uptime ping**: free service at [betterstack.com](https://betterstack.com) or
  [uptimerobot.com](https://uptimerobot.com); point at
  `https://api.jarvisworlds.com/health`. Alerts on the real DB check.
- **OCI cost alerts**: Budgets → Create → "Always-Free guardrail" set to
  $1/mo so you get notified the moment something accidentally provisions
  paid resources.

---

## Known gotchas

- **`Out of Host Capacity` is the #1 reason people give up on Oracle Always
  Free.** Retry persistently, or pick a less-trafficked region at signup.
  Once you have the instance, you keep it — capacity issues are at provision
  time only.
- **The region you pick at signup is locked.** Cross-region migration is
  manual + tedious.
- **Default `iptables` blocks 80/443 even after VCN allows them.**
  Step 3's last bash block is non-optional.
- **A1 is ARM.** Building Docker images on x86 dev machines needs
  `docker buildx build --platform linux/arm64 ...`. Apple Silicon Macs
  build native ARM by default — no extra steps for you.
- **CF Pages + apex CNAME flattening.** Cloudflare automatically
  flattens the apex CNAME — works fine. Some other DNS providers
  don't; if you ever move DNS off CF, use an A record + apex IPs
  Cloudflare publishes.
- **Caddy needs grey-cloud at first.** Orange-cloud breaks HTTP-01
  ACME on the api subdomain.
- **Pre-truncate `api.log` history is gone.** Mentioned in HANDOFF.md;
  noting again in case it matters.

---

## Day-one checklist

- [ ] `jarvisworlds.com` registered at Cloudflare Registrar
- [ ] OCI account created in a low-traffic region (Phoenix / San Jose)
- [ ] A1.Flex 4 OCPU / 24 GB VM provisioned with 100 GB boot vol
- [ ] VCN security list ingress: 80, 443 added
- [ ] Host-level `iptables` rules for 80/443 added + saved
- [ ] SSH hardening (key-only) applied
- [ ] Docker installed; `docker compose version` works
- [ ] GitHub deploy key added to `brianlo06/worldview-api`
- [ ] `/opt/worldview-api` cloned, `.env` filled in with all 9 vars
- [ ] `docker compose up -d --build` succeeded; logs clean
- [ ] DB extensions verified: pgcrypto, plpgsql, postgis, vector
- [ ] Pages project created from `brianlo06/worldview`, `VITE_API_BASE` set
- [ ] DNS records: apex CNAME → Pages, `www` CNAME → Pages, `api` A → VM IP (grey-cloud)
- [ ] Pages custom domains attached: apex + www
- [ ] `curl https://api.jarvisworlds.com/health` returns `{"db":"ok"}`
- [ ] Browser loads `https://jarvisworlds.com` → globe + LIVE indicator
- [ ] Daily `pg_dump` backup cron job in `/etc/cron.d/worldview-backup`
- [ ] OCI Budget guardrail set to $1/mo
- [ ] Calendar reminder: log into OCI console monthly
