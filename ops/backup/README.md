# Spend database offsite backup (ADR-0001, issue #12)

`spend-backup.sh` runs in the `scheduler` service (02:30 US Eastern daily, see `ops/scheduler/crontab`). It runs `pg_dump -Fc` (custom format, compressed) of `hundred_acre_spend` over the network against the Compose Postgres, verifies the archive with `pg_restore -l`, commits it as `hundred_acre_spend.dump` to a clone of the private `hundred-acre-backups` repo, and pushes over HTTPS. If the dump is unchanged nothing is pushed.

## One-time setup

1. Create the private backup repo (outward-facing; done by the user): `gh repo create untastedwooze/hundred-acre-backups --private`.
2. Create a fine-grained token with Contents read/write on `hundred-acre-backups` only, and put it in `.env` as `BACKUP_GH_TOKEN` (with `BACKUP_REPO_URL`, and optionally `BACKUP_ALERT_URL`). See the main README for the exact permissions.
3. Try it once by hand: `docker compose run --rm scheduler bash /opt/hundred-acre/backup/spend-backup.sh`. The state volume is shared, so this also records `last-success`.

## Failure visibility

Failures are never silent. State lives on the `hundred_acre_backup_state` volume, mounted at `/var/lib/hundred-acre-backup` in the scheduler:

- Every run appends to `backup.log` there; scheduled output is also in `docker compose logs scheduler`.
- A failed run writes `FAILED` (removed on the next success), exits non-zero, and, if `BACKUP_ALERT_URL` is set, POSTs a short text message to it.
- `spend-backup-status.sh` exits 1 if the last run failed, there has been no success, or the last success is older than 36h (`MAX_AGE_HOURS`). It is the scheduler's healthcheck, so `docker compose ps` shows `unhealthy` for a broken backup.

## Restore

Restore into the running Compose Postgres using the `migrate` service's image, which has the Postgres 16 client tools and the database URLs. Nothing needs to be installed on the host except Docker and git.

```bash
# 1. get the dump (private repo: gh auth, or a clone URL carrying a token with read access)
gh repo clone untastedwooze/hundred-acre-backups restore-backups     # inside the repo checkout; gitignored below
# 2. restore into a scratch database, check it, drop it
docker compose run --rm -v "$PWD/restore-backups:/restore:ro" migrate bash -c '
  set -e
  SRV="${SPEND_TEST_SERVER_URL/+psycopg/}"; SRV="${SRV%/*}"     # server URL without a database name
  psql "$SRV/postgres" -c "CREATE DATABASE hundred_acre_spend_restore"
  pg_restore --no-owner -d "$SRV/hundred_acre_spend_restore" /restore/hundred_acre_spend.dump
  psql "$SRV/hundred_acre_spend_restore" -c "\dt"
  psql "$SRV/postgres" -c "DROP DATABASE hundred_acre_spend_restore"
'
rm -rf restore-backups
```

For a real restore (for example onto a fresh host after `docker compose up -d` has created an empty `hundred_acre_spend`), stop the services that use it, replace the database, and start again:

```bash
docker compose stop api scheduler
docker compose run --rm -v "$PWD/restore-backups:/restore:ro" migrate bash -c '
  set -e
  SRV="${SPEND_TEST_SERVER_URL/+psycopg/}"; SRV="${SRV%/*}"
  psql "$SRV/postgres" -c "DROP DATABASE hundred_acre_spend" -c "CREATE DATABASE hundred_acre_spend"
  pg_restore --no-owner -d "$SRV/hundred_acre_spend" /restore/hundred_acre_spend.dump
'
docker compose up -d
```

The dump is made with `--no-owner`, so restored objects are owned by the restoring role, `hundred_acre`, which is the intended owner. The dump includes the Alembic version, so `migrate` on the next `up` only applies migrations newer than the backup. Older dumps are in the backup repo's git history.

The scratch-database restore above was tried against a throwaway Compose project on 2026-11-21 and the scratch database was dropped afterwards.
