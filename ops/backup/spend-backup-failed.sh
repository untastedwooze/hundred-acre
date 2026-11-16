#!/usr/bin/env bash
# Run by systemd OnFailure=: surface the failure beyond the service's own journal entry.
msg="hundred-acre Spend backup FAILED. See ~/.local/state/hundred-acre-backup/backup.log"
echo "$msg" | systemd-cat -t hundred-acre-backup -p crit
if command -v notify-send > /dev/null; then notify-send -u critical "hundred-acre backup failed" "$msg" || true; fi
