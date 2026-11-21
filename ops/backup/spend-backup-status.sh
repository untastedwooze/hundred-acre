#!/usr/bin/env bash
# Prints backup health; exit 1 if the last run failed or there was no success in MAX_AGE_HOURS (default 36).
STATE_DIR="${BACKUP_STATE_DIR:-/var/lib/hundred-acre-backup}"
MAX_AGE_HOURS="${MAX_AGE_HOURS:-36}"
if [ -f "$STATE_DIR/FAILED" ]; then echo "FAILED:"; cat "$STATE_DIR/FAILED"; exit 1; fi
if [ ! -f "$STATE_DIR/last-success" ]; then echo "NO SUCCESSFUL BACKUP YET"; exit 1; fi
age=$(( ( $(date +%s) - $(date -d "$(cat "$STATE_DIR/last-success")" +%s) ) / 3600 ))
if [ "$age" -gt "$MAX_AGE_HOURS" ]; then echo "STALE: last success ${age}h ago"; exit 1; fi
echo "OK: last success $(cat "$STATE_DIR/last-success") (${age}h ago)"
