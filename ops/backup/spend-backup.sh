#!/usr/bin/env bash
# Nightly offsite backup of hundred_acre_spend (ADR-0001, issue #12).
# Runs in the scheduler container. Dumps the DB over the network (pg_dump -Fc against SPEND_DATABASE_URL),
# verifies it with pg_restore -l, commits it to a clone of the private backup repo, and pushes over HTTPS
# using BACKUP_GH_TOKEN. On failure writes $BACKUP_STATE_DIR/FAILED and POSTs to BACKUP_ALERT_URL if set.
set -euo pipefail

BACKUP_REPO_URL="${BACKUP_REPO_URL:-}"
BACKUP_GH_TOKEN="${BACKUP_GH_TOKEN:-}"
BACKUP_ALERT_URL="${BACKUP_ALERT_URL:-}"
# libpq wants a plain postgresql:// URL, not SQLAlchemy's postgresql+psycopg://
PG_URL="${SPEND_DATABASE_URL:?SPEND_DATABASE_URL not set}"
PG_URL="${PG_URL/+psycopg/}"
PG_DB="${PG_URL##*/}"
PG_DB="${PG_DB%%\?*}"
STATE_DIR="${BACKUP_STATE_DIR:-/var/lib/hundred-acre-backup}"
CLONE_DIR="${BACKUP_CLONE_DIR:-$STATE_DIR/repo}"
DUMP_NAME="${PG_DB}.dump"
LOG="$STATE_DIR/backup.log"
mkdir -p "$STATE_DIR"

log() { printf '%s %s\n' "$(date -Is)" "$*" | tee -a "$LOG" >&2; }

fail() {
  trap - ERR
  log "BACKUP FAILED: $*"
  { date -Is; echo "$*"; } > "$STATE_DIR/FAILED"
  if [ -n "$BACKUP_ALERT_URL" ]; then
    curl -fsS -m 15 -X POST -H 'Content-Type: text/plain' \
      --data "hundred-acre Spend backup FAILED: $*" "$BACKUP_ALERT_URL" > /dev/null 2>&1 \
      || log "alert POST to BACKUP_ALERT_URL failed"
  fi
  exit 1
}
trap 'fail "unexpected error at line $LINENO"' ERR

[ -n "$BACKUP_REPO_URL" ] || fail "BACKUP_REPO_URL not set"

# The token is handed to git through a credential helper reading the environment, never stored in .git/config.
GIT=(git)
if [ -n "$BACKUP_GH_TOKEN" ]; then
  export BACKUP_GH_TOKEN
  GIT=(git -c credential.helper='!f() { echo username=x-access-token; echo "password=$BACKUP_GH_TOKEN"; }; f')
fi

if [ ! -d "$CLONE_DIR/.git" ]; then
  "${GIT[@]}" clone --quiet "$BACKUP_REPO_URL" "$CLONE_DIR" || fail "clone of $BACKUP_REPO_URL failed"
fi
cd "$CLONE_DIR"
git remote set-url origin "$BACKUP_REPO_URL"
# Sync with remote if it already has commits (a brand-new empty repo has none).
if [ -n "$("${GIT[@]}" ls-remote --heads origin)" ]; then
  "${GIT[@]}" fetch --quiet origin || fail "git fetch failed"
  git reset --quiet --hard FETCH_HEAD || fail "sync with remote failed"
fi

tmp="$(mktemp "$STATE_DIR/dump.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
# -Fc = custom format, already compressed; restore with pg_restore.
pg_dump -Fc --no-owner "$PG_URL" > "$tmp" \
  || fail "pg_dump of $PG_DB failed"
[ -s "$tmp" ] || fail "pg_dump produced an empty file"
# Sanity check: the archive must be readable.
pg_restore -l "$tmp" > /dev/null \
  || fail "dump failed pg_restore --list verification"

mv "$tmp" "$CLONE_DIR/$DUMP_NAME"
git add "$DUMP_NAME"
if git diff --cached --quiet; then
  log "no change in dump; nothing to push"
else
  git -c user.name="hundred-acre-backup" -c user.email="backup@localhost" \
    commit --quiet -m "Backup $PG_DB $(date -Is)" || fail "git commit failed"
  "${GIT[@]}" push --quiet origin HEAD || fail "git push failed"
fi

rm -f "$STATE_DIR/FAILED"
date -Is > "$STATE_DIR/last-success"
log "backup OK ($(stat -c %s "$CLONE_DIR/$DUMP_NAME") bytes)"
