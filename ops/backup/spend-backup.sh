#!/usr/bin/env bash
# Nightly offsite backup of hundred_acre_spend (ADR-0001, issue #12).
# Dumps the DB via pg_dump inside the Postgres docker container, commits the
# compressed dump to a clone of the private backup repo, and pushes.
# Config: ${HUNDRED_ACRE_BACKUP_CONFIG:-~/.config/hundred-acre/backup.env}
# See ops/backup/README.md.
set -euo pipefail

CONFIG="${HUNDRED_ACRE_BACKUP_CONFIG:-$HOME/.config/hundred-acre/backup.env}"
# shellcheck disable=SC1090
[ -f "$CONFIG" ] && . "$CONFIG"

BACKUP_REPO_URL="${BACKUP_REPO_URL:-}"
PG_CONTAINER="${PG_CONTAINER:-scrooge-mcduck-db-1}"
PG_USER="${PG_USER:-app}"
PG_DB="${PG_DB:-hundred_acre_spend}"
STATE_DIR="${BACKUP_STATE_DIR:-$HOME/.local/state/hundred-acre-backup}"
CLONE_DIR="${BACKUP_CLONE_DIR:-$STATE_DIR/repo}"
DUMP_NAME="${PG_DB}.dump"
LOG="$STATE_DIR/backup.log"
mkdir -p "$STATE_DIR"

log() { printf '%s %s\n' "$(date -Is)" "$*" | tee -a "$LOG" >&2; }

fail() {
  trap - ERR
  log "BACKUP FAILED: $*"
  { date -Is; echo "$*"; } > "$STATE_DIR/FAILED"
  exit 1
}
trap 'fail "unexpected error at line $LINENO"' ERR

[ -n "$BACKUP_REPO_URL" ] || fail "BACKUP_REPO_URL not set (see $CONFIG)"

if [ ! -d "$CLONE_DIR/.git" ]; then
  git clone --quiet "$BACKUP_REPO_URL" "$CLONE_DIR" || fail "clone of $BACKUP_REPO_URL failed"
fi
cd "$CLONE_DIR"
git remote set-url origin "$BACKUP_REPO_URL"
# Sync with remote if it already has commits (a brand-new empty repo has none).
if [ -n "$(git ls-remote --heads origin)" ]; then
  git fetch --quiet origin || fail "git fetch failed"
  git reset --quiet --hard FETCH_HEAD || fail "sync with remote failed"
fi

tmp="$(mktemp "$STATE_DIR/dump.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
# -Fc = custom format, already compressed; restore with pg_restore.
docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -Fc --no-owner "$PG_DB" > "$tmp" \
  || fail "pg_dump of $PG_DB in $PG_CONTAINER failed"
[ -s "$tmp" ] || fail "pg_dump produced an empty file"
# Sanity check: the archive must be readable.
docker exec -i "$PG_CONTAINER" pg_restore -l < "$tmp" > /dev/null \
  || fail "dump failed pg_restore --list verification"

mv "$tmp" "$CLONE_DIR/$DUMP_NAME"
git add "$DUMP_NAME"
if git diff --cached --quiet; then
  log "no change in dump; nothing to push"
else
  git -c user.name="hundred-acre-backup" -c user.email="backup@localhost" \
    commit --quiet -m "Backup $PG_DB $(date -Is)" || fail "git commit failed"
  git push --quiet origin HEAD || fail "git push failed"
fi

rm -f "$STATE_DIR/FAILED"
date -Is > "$STATE_DIR/last-success"
log "backup OK ($(stat -c %s "$CLONE_DIR/$DUMP_NAME") bytes)"
