#!/usr/bin/env bash
# SLA sweep for the VPS install. On Vercel the same endpoint is called by Vercel Cron.
# Calls GET /api/cron/sla with the CRON_SECRET bearer token read from the app env file.
# The token is piped to curl on stdin, so it never appears in the process list.
#
# Installed by setup_vps.sh as a cron entry every 5 minutes.

set -euo pipefail

ENV_FILE="${ENV_FILE:-/opt/triageops/.env}"
DOMAIN="${DOMAIN:-$(grep -E '^DOMAIN=' /opt/triageops/deploy/.env 2>/dev/null | tail -n 1 | cut -d= -f2-)}"

[[ -n "$DOMAIN" ]] || { echo "sla_cron: DOMAIN is not set" >&2; exit 1; }
SECRET="$(grep -E '^CRON_SECRET=' "$ENV_FILE" | tail -n 1 | cut -d= -f2- | tr -d '"'"'")"
[[ -n "$SECRET" ]] || { echo "sla_cron: CRON_SECRET is empty in $ENV_FILE" >&2; exit 1; }

printf 'Authorization: Bearer %s\n' "$SECRET" \
  | curl -fsS --max-time 30 -H @- "https://$DOMAIN/api/cron/sla"
echo
