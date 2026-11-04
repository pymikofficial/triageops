# TriageOps VPS kit

The live demo runs on Vercel serverless functions in London (lhr1) with Supabase Postgres in London (eu-west-2). This folder runs the same application on any Ubuntu 24.04 VPS (Hetzner, DigitalOcean, OVH, Linode) for a client that wants it on its own server, in its own region, behind its own firewall.

```text
Internet ──► UFW (22, 80, 443 only)
              │
              ├─► nginx container   TLS 1.2/1.3, HSTS, gzip, security headers,
              │     :80  :443       rate limits on /api/demo and /api/chat,
              │                     static SPA from /usr/share/nginx/html
              │        │
              │        └─► app container (internal network only, port 8000 never published)
              │              uvicorn triageops.app:app, non-root, read-only filesystem
              │                    │
              │                    ├─► Supabase Postgres (or any Postgres 17 + pgvector)
              │                    ├─► Claude API
              │                    └─► Supabase Edge Function "embed"
              │
              └─► certbot container  renews Let's Encrypt certificates twice a day (webroot)

cron (host)   02:30 backup.sh     pg_dump | gzip, 14 day retention, optional off-site hook
              */5   sla_cron.sh   GET /api/cron/sla with the CRON_SECRET bearer token
```

## Files

| File | Purpose |
|---|---|
| `Dockerfile` | Multi-stage. `app` target is python:3.12-slim with a non-root user running uvicorn. `web` target builds the Vite frontend and copies `dist` into nginx. |
| `Dockerfile.dockerignore` | Keeps `.env`, `.git`, `node_modules` and local state out of the build context. |
| `docker-compose.yml` | `app`, `nginx` and `certbot` services, `restart: unless-stopped`, healthcheck on `/api/health`, log rotation. |
| `nginx/triageops.conf` | Template rendered with `DOMAIN`, `APP_UPSTREAM` and `WEB_ROOT`. HTTP to HTTPS redirect, ACME challenge, TLS, HSTS, CSP, gzip, SPA fallback, `/api/` proxy, rate limit zones, 1 MB body limit. |
| `systemd/triageops.service` | Runs uvicorn without Docker under a dedicated user, with systemd sandboxing. |
| `setup_vps.sh` | Idempotent bootstrap for a fresh server. Takes the domain and the Let's Encrypt email. |
| `backup.sh` | Nightly `pg_dump` to `/var/backups/triageops`, gzip, integrity check, 14 day retention, upload hook. |
| `sla_cron.sh` | Calls the SLA sweep endpoint every 5 minutes (Vercel Cron does this on the serverless deployment). |

## 15 minute runbook

You need a domain you control, an SSH key pair, and the values for `.env` (see `.env.example` in the repository root). The database must already have every file in `supabase/migrations/` applied in order, `001_init.sql` to `005_rmm_channel.sql`.

**Minute 0 to 2. Create the server.** Ubuntu 24.04 LTS with 2 vCPU and 2 to 4 GB RAM is plenty (a small Hetzner Cloud server or DigitalOcean droplet). Add your SSH public key at creation time so it lands in `/root/.ssh/authorized_keys`. Note the public IPv4 address.

**Minute 2 to 4. Point DNS at it.** At your DNS provider create an A record for the hostname (for example `triageops` under `example.co.uk`) with the server IPv4 address and a TTL of 300 seconds. Add an AAAA record only if the server has IPv6. If the zone is on Cloudflare, leave the record as DNS only (grey cloud) until the first certificate is issued. Check from your laptop until it resolves.

```bash
dig +short triageops.example.co.uk A
```

**Minute 4 to 6. Copy the script and your filled env file.**

```bash
scp triageops.env root@203.0.113.10:/root/triageops.env
ssh root@203.0.113.10
curl -fsSLO https://raw.githubusercontent.com/pymikofficial/triageops/main/deploy/setup_vps.sh
chmod +x setup_vps.sh
```

**Minute 6 to 12. Run the bootstrap.**

```bash
ENV_SOURCE=/root/triageops.env ./setup_vps.sh triageops.example.co.uk ops@example.co.uk
```

The script works through numbered steps and prints each one. In order it updates packages, creates the `deploy` user with sudo, copies the root SSH key to it, disables password and root SSH login (only after confirming a key is in place), enables UFW for OpenSSH, 80 and 443, enables fail2ban and unattended upgrades, installs Docker, clones the repository to `/opt/triageops`, installs `.env` with mode 600, checks that DNS points at this server, obtains the certificate with certbot in webroot mode, starts the stack and installs the cron jobs. It finishes by calling `https://<domain>/api/health`.

If you run it without `ENV_SOURCE`, it copies `.env.example` to `/opt/triageops/.env`, stops, and asks you to fill it in and run the same command again. Use `CERTBOT_STAGING=1` while testing so you do not hit Let's Encrypt rate limits.

**Minute 12 to 14. Verify from a second terminal.** Keep the root session open until this works.

```bash
ssh deploy@triageops.example.co.uk
curl -s https://triageops.example.co.uk/api/health
curl -sI https://triageops.example.co.uk | grep -iE 'strict-transport|content-security'
ssh root@triageops.example.co.uk
```

The health call returns `"ok":true` with `db` and `ai` both true. The last command must fail with `Permission denied (publickey)`. Then send a signed ticket from your laptop with `python scripts/send_test_ticket.py --url https://triageops.example.co.uk` (with `WEBHOOK_SECRET` exported) and watch it appear on the dashboard.

**Minute 14 to 15. Clean up.**

```bash
shred -u /root/triageops.env
sudo ufw status verbose
sudo fail2ban-client status sshd
```

## Day two operations

| Task | Command (as `deploy`, in `/opt/triageops/deploy`) |
|---|---|
| Deploy a new version | `git -C /opt/triageops pull --ff-only` then `docker compose up -d --build` |
| Status and health | `docker compose ps` |
| Application logs | `docker compose logs -f --tail=200 app` |
| Test certificate renewal | `docker compose run --rm --entrypoint certbot certbot renew --dry-run --webroot -w /var/www/certbot` |
| Run a backup now | `/opt/triageops/deploy/backup.sh` |
| List backups | `ls -lh /var/backups/triageops` |
| Restore into a scratch database | `gunzip -c /var/backups/triageops/<file>.sql.gz \| psql "$RESTORE_URL"` |
| Rotate a secret | edit `/opt/triageops/.env`, then `docker compose up -d app` |

Restore into an empty Postgres 17 database that already has `create extension vector with schema extensions`, never over the live one. A restore drill once a month is the only way to know the backups work.

`backup.sh` reads `DATABASE_URL` from `/opt/triageops/.env`. Point it at a session connection (Supabase session pooler on port 5432 or the direct connection), because `pg_dump` does not work reliably through the transaction pooler on port 6543 that the app uses. The role must read every table and sequence and bypass row level security (the Supabase `postgres` role does). `triage_api` deliberately cannot, and the dump then fails loudly rather than writing empty tables. For off-site copies set `BACKUP_UPLOAD_HOOK` to an executable that receives the file path, for example a two line wrapper around `rclone copy` or `aws s3 cp` with a bucket lifecycle rule.

## Without Docker

`systemd/triageops.service` runs the API directly under a `triageops` system user with `NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, an empty capability set and a syscall filter. The env file stays `root:root 600` because systemd reads `EnvironmentFile` before it drops privileges. Install nginx from the Ubuntu archive and render the same site template.

```bash
sudo apt-get install -y nginx certbot
DOMAIN=triageops.example.co.uk APP_UPSTREAM=127.0.0.1:8000 WEB_ROOT=/var/www/triageops envsubst '${DOMAIN} ${APP_UPSTREAM} ${WEB_ROOT}' < nginx/triageops.conf | sudo tee /etc/nginx/sites-available/triageops.conf
sudo ln -s /etc/nginx/sites-available/triageops.conf /etc/nginx/sites-enabled/triageops.conf
sudo nginx -t
sudo systemctl reload nginx
```

Issue the certificate with `certbot certonly --webroot -w /var/www/certbot -d <domain>` before enabling the HTTPS server block, copy the built `dist` folder to `/var/www/triageops`, then enable the unit with `systemctl enable --now triageops`.

## Security notes

1. Docker writes its own iptables rules and can bypass UFW for published ports. Only 80 and 443 are published here, and both are allowed in UFW anyway. The app port is internal to the compose network.
2. Membership of the `docker` group is equivalent to root. Only `deploy` is in it, and `deploy` logs in with a key only.
3. nginx overwrites `X-Forwarded-For` with the real client address rather than appending, so clients cannot spoof their IP to dodge the per-IP rate limits.
4. Secrets live only in `/opt/triageops/.env` (mode 600). They are never baked into an image, never passed on a command line, and `.env` is excluded from the build context.
5. OCSP stapling is deliberately off. Let's Encrypt retired its OCSP service in 2025.
