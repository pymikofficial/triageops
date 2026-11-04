#!/usr/bin/env bash
# Nightly logical backup of the TriageOps database.
#
#   pg_dump (public schema) | gzip  ->  /var/backups/triageops/triageops-YYYYmmdd-HHMMSS.sql.gz
#   keeps 14 days, then deletes older files
#   optional upload hook for off-site copies
#
# Reads from the app env file (default /opt/triageops/.env):
#   DATABASE_URL         preferred. Use a session connection (Supabase session pooler on port 5432
#                        or the direct connection), because pg_dump does not work reliably through
#                        the transaction pooler on port 6543. The role must read every table and
#                        sequence and bypass RLS, which triage_api deliberately cannot.
#   DATABASE_URLS        fallback, first URL in the list
#   BACKUP_UPLOAD_HOOK   optional executable, called with the backup path, e.g. an rclone or
#                        aws s3 cp wrapper. A failed upload is logged but keeps the local copy.
#
# Runs pg_dump from the postgres:17 image when Docker is available, so the client always
# matches the Supabase server major version (Ubuntu 24.04 ships pg_dump 16, which refuses a 17 server).

set -euo pipefail

ENV_FILE="${ENV_FILE:-/opt/triageops/.env}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/triageops}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
PG_IMAGE="${PG_IMAGE:-postgres:17-alpine}"

log() { printf '%s backup: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR $*"; exit 1; }

# Read KEY=value from the env file without sourcing it (values may contain shell characters).
env_value() {
  local key="$1" line
  line="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -n 1 || true)"
  line="${line#*=}"
  line="${line%\"}"; line="${line#\"}"
  line="${line%\'}"; line="${line#\'}"
  printf '%s' "$line"
}

[[ -r "$ENV_FILE" ]] || die "cannot read $ENV_FILE"

DB_URL="${DATABASE_URL:-$(env_value DATABASE_URL)}"
if [[ -z "$DB_URL" ]]; then
  URLS="$(env_value DATABASE_URLS)"
  DB_URL="${URLS%%,*}"
fi
[[ -n "$DB_URL" ]] || die "DATABASE_URL (or DATABASE_URLS) is not set in $ENV_FILE"
[[ "$DB_URL" == *":6543"* ]] && log "warning, port 6543 is the transaction pooler. Set DATABASE_URL to a session connection if the dump fails."

UPLOAD_HOOK="${BACKUP_UPLOAD_HOOK:-$(env_value BACKUP_UPLOAD_HOOK)}"

umask 077
install -d -m 700 "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
TARGET="$BACKUP_DIR/triageops-$STAMP.sql.gz"
PARTIAL="$TARGET.partial"
trap 'rm -f "$PARTIAL"' EXIT

# No --enable-row-security on purpose. A role that RLS would filter makes pg_dump fail loudly
# instead of writing a backup with empty tables. Use a role with BYPASSRLS (Supabase "postgres").
DUMP_ARGS=(--schema=public --no-owner --no-privileges --quote-all-identifiers)

log "starting dump to $TARGET"
if command -v docker >/dev/null 2>&1; then
  # "-e DB_URL" without a value copies it from this environment, so the URL (and its
  # password) never appears in the process list of the host.
  DB_URL="$DB_URL" docker run --rm -e DB_URL "$PG_IMAGE" \
    sh -c 'exec pg_dump "$DB_URL" "$@"' pg_dump "${DUMP_ARGS[@]}" | gzip -9 > "$PARTIAL"
elif command -v pg_dump >/dev/null 2>&1; then
  # Fallback only. The client major version must be >= the server (17 for Supabase).
  PGCONNECT_TIMEOUT=10 pg_dump "${DUMP_ARGS[@]}" --dbname="$DB_URL" | gzip -9 > "$PARTIAL"
else
  die "neither docker nor pg_dump is installed"
fi

# A valid dump is non-trivial and gzip-clean. Anything else is treated as a failure.
gzip -t "$PARTIAL" || die "gzip integrity check failed"
BYTES="$(stat -c %s "$PARTIAL")"
(( BYTES > 200 )) || die "dump is suspiciously small (${BYTES} bytes)"
# zcat is wrapped so its SIGPIPE (grep -q exits early) does not trip pipefail.
{ zcat "$PARTIAL" || true; } | grep -q 'CREATE TABLE "public"."tickets"' || die "dump does not contain the tickets table"
mv "$PARTIAL" "$TARGET"
trap - EXIT
log "wrote $TARGET (${BYTES} bytes)"

if [[ -n "$UPLOAD_HOOK" ]]; then
  if [[ -x "$UPLOAD_HOOK" ]]; then
    if "$UPLOAD_HOOK" "$TARGET"; then
      log "upload hook succeeded"
    else
      log "warning, upload hook failed, local copy kept"
    fi
  else
    log "warning, BACKUP_UPLOAD_HOOK $UPLOAD_HOOK is not executable, skipped"
  fi
fi

DELETED="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'triageops-*.sql.gz' -mtime +"$((RETENTION_DAYS - 1))" -print -delete | wc -l)"
log "retention ${RETENTION_DAYS} days, removed ${DELETED} old file(s)"
log "done"
