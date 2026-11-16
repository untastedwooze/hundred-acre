# Spend database offsite backup (ADR-0001, issue #12)

`spend-backup.sh` runs `pg_dump -Fc` (custom format, compressed) of `hundred_acre_spend` inside the Postgres container (`scrooge-mcduck-db-1`; psql/pg_dump are not on the host PATH), verifies the archive with `pg_restore -l`, commits it as `hundred_acre_spend.dump` to a local clone of a private GitHub repo, and pushes. If the dump is unchanged nothing is pushed.

## One-time setup

1. Create the private backup repo (outward-facing; done by the user):

   ```bash
   gh repo create untastedwooze/hundred-acre-backups --private
   ```

2. Config file `~/.config/hundred-acre/backup.env`:

   ```bash
   BACKUP_REPO_URL=git@github.com:untastedwooze/hundred-acre-backups.git
   # optional overrides: PG_CONTAINER, PG_USER (app), PG_DB, BACKUP_STATE_DIR, BACKUP_CLONE_DIR
   ```

   The machine needs git push credentials for that URL that work non-interactively (SSH key or `gh auth setup-git` for HTTPS).

3. Try it once by hand: `ops/backup/spend-backup.sh`.

4. Install the timer (systemd user units; repo assumed at `~/honey-tree/hundred-acre`, edit `ExecStart=` otherwise):

   ```bash
   mkdir -p ~/.config/systemd/user
   cp ops/backup/*.service ops/backup/*.timer ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now hundred-acre-spend-backup.timer
   loginctl enable-linger "$USER"   # optional: run even when not logged in
   ```

The timer fires at 02:30 daily with `Persistent=true`, so a run missed while the machine was off happens at the next boot/login.

## Failure visibility

Failures are never silent:

- Every run appends to `~/.local/state/hundred-acre-backup/backup.log`.
- A failed run writes `~/.local/state/hundred-acre-backup/FAILED` (removed on the next success), exits non-zero (`journalctl --user -u hundred-acre-spend-backup`), and triggers `hundred-acre-spend-backup-failed.service`, which logs at `crit` priority and sends a desktop notification if `notify-send` exists.
- `ops/backup/spend-backup-status.sh` exits 1 if the last run failed, there has been no success, or the last success is older than 36h (`MAX_AGE_HOURS`). Run it from a shell prompt hook or a future app health check to catch a timer that silently stopped.

## Restore

```bash
git clone git@github.com:untastedwooze/hundred-acre-backups.git /tmp/ha-restore   # or use the existing clone in ~/.local/state/hundred-acre-backup/repo
docker exec scrooge-mcduck-db-1 psql -U app -d app -c "CREATE DATABASE hundred_acre_spend_restore"   # scratch DB; for a real restore use hundred_acre_spend (CREATE ... OWNER hundred_acre)
docker exec -i scrooge-mcduck-db-1 pg_restore -U app -d hundred_acre_spend_restore --no-owner < /tmp/ha-restore/hundred_acre_spend.dump
# verify, then drop the scratch DB:
docker exec scrooge-mcduck-db-1 psql -U app -d hundred_acre_spend_restore -c '\dt'
docker exec scrooge-mcduck-db-1 psql -U app -d app -c "DROP DATABASE hundred_acre_spend_restore"
```

For a real restore into an empty `hundred_acre_spend`, restore as above and then `GRANT`/`ALTER ... OWNER TO hundred_acre` (the dump uses `--no-owner`, so objects are owned by the restoring user), e.g. restore with `-U hundred_acre` after creating the DB with `OWNER hundred_acre`. Older versions of the dump are in the backup repo's git history.

This restore was tried by hand on 2026-11-16 into a scratch database, which was then dropped.
