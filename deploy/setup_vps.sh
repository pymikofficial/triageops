#!/usr/bin/env bash
# TriageOps VPS bootstrap for a fresh Ubuntu 24.04 server (Hetzner, DigitalOcean, any KVM VPS).
#
# Usage (as root, after the DNS A record points at this server):
#   sudo ./setup_vps.sh <domain> <letsencrypt-email>
#
# Optional environment overrides:
#   DEPLOY_USER=deploy                     non-root user that owns and runs the app
#   APP_DIR=/opt/triageops                 checkout location
#   REPO_URL=https://github.com/pymikofficial/triageops.git
#   BRANCH=main
#   ENV_SOURCE=/root/triageops.env         a filled .env to install (otherwise .env.example is copied)
#   CERTBOT_STAGING=1                      use the Let's Encrypt staging CA while testing
#
# Safe to re-run. Every step checks the current state before changing anything.

set -euo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-}"
DEPLOY_USER="${DEPLOY_USER:-deploy}"
APP_DIR="${APP_DIR:-/opt/triageops}"
REPO_URL="${REPO_URL:-https://github.com/pymikofficial/triageops.git}"
BRANCH="${BRANCH:-main}"
ENV_SOURCE="${ENV_SOURCE:-}"
CERTBOT_STAGING="${CERTBOT_STAGING:-0}"
BACKUP_DIR="/var/backups/triageops"
WEBROOT="/var/www/certbot"

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33m    WARNING %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mERROR %s\033[0m\n' "$*" >&2; exit 1; }

as_deploy() { sudo -u "$DEPLOY_USER" -H "$@"; }

# ---------------------------------------------------------------------------
step "0/12 Preflight checks"
[[ $EUID -eq 0 ]] || die "Run as root (sudo ./setup_vps.sh <domain> <email>)."
[[ -n "$DOMAIN" && -n "$EMAIL" ]] || die "Usage: sudo ./setup_vps.sh <domain> <letsencrypt-email>"
[[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || die "DOMAIN '$DOMAIN' does not look like a hostname."
[[ "$EMAIL" == *@*.* ]] || die "EMAIL '$EMAIL' does not look like an email address."
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ "${ID:-}" == "ubuntu" && "${VERSION_ID:-}" == "24.04" ]] || warn "Tested on Ubuntu 24.04, found ${PRETTY_NAME:-unknown}."
fi
info "domain=$DOMAIN user=$DEPLOY_USER dir=$APP_DIR branch=$BRANCH"

# ---------------------------------------------------------------------------
step "1/12 System packages and security updates"
export DEBIAN_FRONTEND=noninteractive
APT_OPTS=(-y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
apt-get update -q
apt-get "${APT_OPTS[@]}" upgrade
apt-get "${APT_OPTS[@]}" install ca-certificates curl git gnupg ufw fail2ban python3-systemd \
  unattended-upgrades apt-listchanges cron

# ---------------------------------------------------------------------------
step "2/12 Non-root deploy user '$DEPLOY_USER' with sudo"
if id "$DEPLOY_USER" >/dev/null 2>&1; then
  info "User exists."
else
  adduser --disabled-password --gecos "" "$DEPLOY_USER"
  info "User created."
fi
usermod -aG sudo "$DEPLOY_USER"
# Key-only login means the user has no password, so sudo is passwordless for this one user.
SUDOERS_FILE="/etc/sudoers.d/90-$DEPLOY_USER"
if [[ ! -f "$SUDOERS_FILE" ]]; then
  printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$DEPLOY_USER" > "$SUDOERS_FILE.tmp"
  chmod 440 "$SUDOERS_FILE.tmp"
  visudo -cf "$SUDOERS_FILE.tmp" >/dev/null
  mv "$SUDOERS_FILE.tmp" "$SUDOERS_FILE"
fi

# ---------------------------------------------------------------------------
step "3/12 SSH key for '$DEPLOY_USER'"
DEPLOY_HOME="$(getent passwd "$DEPLOY_USER" | cut -d: -f6)"
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$DEPLOY_HOME/.ssh"
AUTH_KEYS="$DEPLOY_HOME/.ssh/authorized_keys"
touch "$AUTH_KEYS"
if [[ -s /root/.ssh/authorized_keys ]]; then
  while IFS= read -r key; do
    [[ -z "$key" || "$key" == \#* ]] && continue
    grep -qxF "$key" "$AUTH_KEYS" || printf '%s\n' "$key" >> "$AUTH_KEYS"
  done < /root/.ssh/authorized_keys
fi
chown "$DEPLOY_USER:$DEPLOY_USER" "$AUTH_KEYS"
chmod 600 "$AUTH_KEYS"
# Refuse to disable password login unless a key is definitely in place (no lock-out).
grep -qE '^(ssh-(ed25519|rsa)|ecdsa-sha2-|sk-)' "$AUTH_KEYS" \
  || die "No SSH public key for $DEPLOY_USER. Add one to /root/.ssh/authorized_keys and re-run."
info "$(grep -cE '^(ssh-|ecdsa-|sk-)' "$AUTH_KEYS") key(s) installed."

# ---------------------------------------------------------------------------
step "4/12 Harden sshd (keys only, no root login)"
# 00- prefix matters. sshd keeps the FIRST value it reads, and cloud images ship
# 50-cloud-init.conf with PasswordAuthentication yes.
cat > /etc/ssh/sshd_config.d/00-triageops-hardening.conf <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
AuthenticationMethods publickey
PermitEmptyPasswords no
X11Forwarding no
MaxAuthTries 3
LoginGraceTime 30
ClientAliveInterval 300
ClientAliveCountMax 2
EOF
sshd -t || die "sshd config test failed, not reloading."
systemctl reload ssh 2>/dev/null || systemctl restart ssh
info "Keep this session open and test 'ssh $DEPLOY_USER@$DOMAIN' from a second terminal."

# ---------------------------------------------------------------------------
step "5/12 Firewall (UFW) allows OpenSSH, 80 and 443 only"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null
ufw status verbose | sed 's/^/    /'
info "Docker publishes only 80 and 443. The app port 8000 is never published, so UFW and Docker agree."

# ---------------------------------------------------------------------------
step "6/12 fail2ban for SSH"
cat > /etc/fail2ban/jail.d/triageops.local <<'EOF'
[DEFAULT]
bantime  = 1h
findtime = 10m
maxretry = 5
backend  = systemd

[sshd]
enabled = true
port    = ssh
EOF
systemctl enable --now fail2ban >/dev/null
systemctl restart fail2ban
info "fail2ban active, sshd jail enabled."

# ---------------------------------------------------------------------------
step "7/12 Automatic security updates"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
systemctl enable --now unattended-upgrades >/dev/null
info "unattended-upgrades enabled."

# ---------------------------------------------------------------------------
step "8/12 Docker Engine and Compose plugin"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  info "Docker already installed ($(docker --version))."
else
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck disable=SC1091
  CODENAME="$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $CODENAME stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get "${APT_OPTS[@]}" install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
if [[ ! -f /etc/docker/daemon.json ]]; then
  cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" },
  "live-restore": true
}
EOF
  systemctl restart docker
fi
systemctl enable --now docker >/dev/null
usermod -aG docker "$DEPLOY_USER"
info "Note: the docker group is root-equivalent. Only $DEPLOY_USER is in it."

# ---------------------------------------------------------------------------
step "9/12 Application checkout at $APP_DIR"
if [[ -d "$APP_DIR/.git" ]]; then
  as_deploy git -C "$APP_DIR" fetch --quiet origin "$BRANCH"
  as_deploy git -C "$APP_DIR" checkout --quiet "$BRANCH"
  as_deploy git -C "$APP_DIR" pull --quiet --ff-only origin "$BRANCH"
  info "Updated to $(as_deploy git -C "$APP_DIR" rev-parse --short HEAD)."
else
  install -d -m 750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$APP_DIR"
  as_deploy git clone --quiet --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
  info "Cloned $REPO_URL ($BRANCH)."
fi

# ---------------------------------------------------------------------------
step "10/12 Environment files (chmod 600)"
APP_ENV="$APP_DIR/.env"
COMPOSE_ENV="$APP_DIR/deploy/.env"
if [[ -n "$ENV_SOURCE" ]]; then
  [[ -f "$ENV_SOURCE" ]] || die "ENV_SOURCE $ENV_SOURCE not found."
  install -m 600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$ENV_SOURCE" "$APP_ENV"
  info "Installed $ENV_SOURCE as $APP_ENV."
elif [[ ! -f "$APP_ENV" ]]; then
  install -m 600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$APP_DIR/.env.example" "$APP_ENV"
  warn "$APP_ENV was created from .env.example and has no secrets yet."
  warn "Fill it in (sudo -u $DEPLOY_USER nano $APP_ENV), then re-run this script."
  exit 0
fi
chown "$DEPLOY_USER:$DEPLOY_USER" "$APP_ENV"
chmod 600 "$APP_ENV"
for key in ANTHROPIC_API_KEY DATABASE_URLS WEBHOOK_SECRET IP_HASH_SALT; do
  grep -qE "^${key}=.+" "$APP_ENV" || warn "$key is empty in $APP_ENV."
done
umask 077
printf 'DOMAIN=%s\nLETSENCRYPT_EMAIL=%s\n' "$DOMAIN" "$EMAIL" > "$COMPOSE_ENV"
chown "$DEPLOY_USER:$DEPLOY_USER" "$COMPOSE_ENV"
chmod 600 "$COMPOSE_ENV"
umask 022
info "App secrets in $APP_ENV, compose settings in $COMPOSE_ENV, both 600."

# ---------------------------------------------------------------------------
step "11/12 TLS certificate from Let's Encrypt (certbot webroot)"
install -d -m 755 "$WEBROOT" /etc/letsencrypt
CERT="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
if [[ -f "$CERT" ]]; then
  info "Certificate already present, renewals run in the certbot container."
else
  PUBLIC_IP="$(curl -fsS4 --max-time 5 https://api.ipify.org || true)"
  DNS_IP="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}' || true)"
  if [[ -n "$PUBLIC_IP" && "$DNS_IP" != "$PUBLIC_IP" ]]; then
    warn "DNS for $DOMAIN resolves to '${DNS_IP:-nothing}' but this server is $PUBLIC_IP."
    warn "Create the A record first (see deploy/README.md), wait for it to resolve, then re-run."
    die "DNS not ready."
  fi
  STAGING_FLAG=()
  [[ "$CERTBOT_STAGING" == "1" ]] && STAGING_FLAG=(--staging)
  # Temporary HTTP server for the ACME challenge, because the real nginx config needs the cert to start.
  docker rm -f triageops-acme >/dev/null 2>&1 || true
  (cd "$APP_DIR/deploy" && as_deploy docker compose stop nginx >/dev/null 2>&1 || true)
  docker run -d --rm --name triageops-acme -p 80:80 -v "$WEBROOT:/usr/share/nginx/html:ro" nginx:1.27-alpine >/dev/null
  trap 'docker rm -f triageops-acme >/dev/null 2>&1 || true' EXIT
  docker run --rm -v /etc/letsencrypt:/etc/letsencrypt -v "$WEBROOT:$WEBROOT" certbot/certbot:latest \
    certonly --webroot -w "$WEBROOT" -d "$DOMAIN" --email "$EMAIL" \
    --agree-tos --no-eff-email --non-interactive "${STAGING_FLAG[@]}"
  docker rm -f triageops-acme >/dev/null 2>&1 || true
  trap - EXIT
  [[ -f "$CERT" ]] || die "certbot finished but $CERT is missing."
  info "Certificate issued for $DOMAIN."
fi

# ---------------------------------------------------------------------------
step "12/12 Start the stack, nightly backups and the SLA cron"
(cd "$APP_DIR/deploy" && as_deploy docker compose up -d --build --remove-orphans)

install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$BACKUP_DIR"
for logfile in /var/log/triageops-backup.log /var/log/triageops-sla.log; do
  touch "$logfile"
  chown "$DEPLOY_USER:$DEPLOY_USER" "$logfile"
  chmod 640 "$logfile"
done
chmod 750 "$APP_DIR/deploy/backup.sh" "$APP_DIR/deploy/sla_cron.sh"
cat > /etc/cron.d/triageops <<EOF
# TriageOps scheduled jobs. Managed by setup_vps.sh, re-running the script rewrites this file.
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# Nightly pg_dump, 14 day retention.
30 2 * * * $DEPLOY_USER $APP_DIR/deploy/backup.sh >> /var/log/triageops-backup.log 2>&1
# SLA sweep every 5 minutes (Vercel Cron does this on the serverless deployment).
*/5 * * * * $DEPLOY_USER $APP_DIR/deploy/sla_cron.sh >/dev/null 2>> /var/log/triageops-sla.log
EOF
chmod 644 /etc/cron.d/triageops
rm -f /etc/cron.d/triageops-backup
info "Cron installed. Backups 02:30 server time into $BACKUP_DIR, SLA sweep every 5 minutes."

info "Waiting for the API to report healthy..."
for _ in $(seq 1 30); do
  if curl -fsS --max-time 5 "https://$DOMAIN/api/health" >/dev/null 2>&1; then
    curl -fsS --max-time 5 "https://$DOMAIN/api/health"; echo
    step "Done. TriageOps is live at https://$DOMAIN"
    exit 0
  fi
  sleep 4
done
warn "Health check did not pass yet. Inspect with:"
warn "  cd $APP_DIR/deploy && docker compose ps && docker compose logs --tail=100 app"
exit 1
