# hundred-acre
A fullstack, vibe-coded, personal investing application and backtester where the cost to implement every feature is transparent in the web interface.

## Run with Docker Compose

Compose is the one way to run hundred-acre. Prerequisites: Docker with the Compose plugin. Images are built locally; there is no registry.

```bash
cp .env.example .env     # then fill it in, see below
docker compose up -d     # builds the images, then: postgres -> migrate -> api, request-engine, scheduler -> web
```

`migrate` creates both databases (`hundred_acre_market` and `hundred_acre_spend`, kept separate per ADR-0001) if missing and migrates them to head on every `up`; the backend services start only after it succeeds. The app is at `http://localhost:8100`. `docker compose ps` shows health, `docker compose logs -f scheduler` shows scheduled job output, and `docker compose down` stops everything (data lives in named volumes and `HUNDRED_ACRE_DATA_DIR`, so it survives).

### `.env` settings

`.env` is gitignored; `.env.example` has a safe dev default for everything except the secrets.

| Setting | Default | Meaning |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | `hundred_acre` | Password of the `hundred_acre` role. Applies only when the `hundred_acre_pg` volume is first created. |
| `POSTGRES_BIND` / `POSTGRES_PORT` | `127.0.0.1` / `5433` | Where Postgres is published on the host (for the dev loop and `pg_dump`). Keep it on localhost. |
| `WEB_BIND` / `WEB_PORT` | `127.0.0.1` / `8100` | Where the web app is published. `WEB_BIND=0.0.0.0` opens it to your LAN; there is no authentication. |
| `HUNDRED_ACRE_DATA_DIR` | `/srv/hundred-acre-data` | Absolute host directory for market files, see below. Required. |
| `GH_TOKEN_READ` | blank | Read-only GitHub token the API uses to read Features. Blank: Spend views report "GitHub unavailable". |
| `MASSIVE_API_KEY` | blank | Massive API key for the Request Engine. Never written to rows, files, logs or errors. |
| `MASSIVE_CALLS_PER_MINUTE` / `MASSIVE_MAX_CONCURRENT_CALLS` | `5` / `1` | Request Engine rate limits (free tier: 5 Calls/min). |
| `BACKUP_REPO_URL` | `https://github.com/untastedwooze/hundred-acre-backups.git` | HTTPS URL of the private Spend backup repo. |
| `BACKUP_GH_TOKEN` | blank | Token the scheduler uses to push the Spend backup. Without it the backup cannot push and the scheduler turns unhealthy. |
| `BACKUP_ALERT_URL` | blank | Optional. A failed backup POSTs a short text message here (for example an ntfy topic URL). |

### GitHub tokens

Create two fine-grained personal access tokens (GitHub > Settings > Developer settings > Personal access tokens > Fine-grained tokens). Give each the minimum below and nothing else, so the internet-facing API never holds write access.

| Token | Repository access | Permissions |
| --- | --- | --- |
| `GH_TOKEN_READ` | Only `untastedwooze/hundred-acre` | Issues: Read-only (Metadata read is added automatically) |
| `BACKUP_GH_TOKEN` | Only `untastedwooze/hundred-acre-backups` | Contents: Read and write (Metadata read is added automatically) |

`hundred-acre-backups` is a private repo you create once: `gh repo create untastedwooze/hundred-acre-backups --private`.

### Market files and `HUNDRED_ACRE_DATA_DIR`

The DuckLake Parquet files (`$HUNDRED_ACRE_DATA_DIR/lake`) and the Massive response bodies (`$HUNDRED_ACRE_DATA_DIR/responses`) live in this one host directory, side by side so the offsite Parquet sync can exclude `responses/` (`ops/backup/parquet-sync.exclude`). The directory is mounted at the same absolute path inside `api`, `request-engine` and `scheduler`, because DuckLake records its data path in the catalog: with the same path everywhere, a host-side `uv run market ...` (with `MARKET_DATA_PATH=$HUNDRED_ACRE_DATA_DIR/lake`) and the containers agree on where the files are. Containers write as root, so files they create in this directory (`lake/`, `responses/`) are owned by root and a host-side `uv run market ...` running as your user cannot write to them (DuckLake fails to add Parquet files). Create the directory before the first `up` and, after the containers have created `lake/` and `responses/`, hand them to your user, e.g. `sudo chown -R "$USER": $HUNDRED_ACRE_DATA_DIR` (root-run containers ignore ownership, so they keep working), or make the tree group/world-writable with `sudo chmod -R a+rwX $HUNDRED_ACRE_DATA_DIR`. New files the containers write later are root-owned again, so repeat after the first container write, or run the services as your host uid (`user: "${UID}:${GID}"` on `api`, `request-engine` and `scheduler`, with `UID`/`GID` exported in your shell) if you work from the host regularly. On snap-packaged Docker, bind mounts cannot reach `/tmp` or hidden directories, so choose a normal path.

### Schedule

The `scheduler` service runs the committed crontab `ops/scheduler/crontab` in US Eastern time (follows daylight saving): `reference-sync` 01:00 Tue-Sat, `nightly` 02:00 Tue-Sat, `weekly` 06:00 Sat, and the Spend backup 02:30 daily. `backfill` is not scheduled.

### One-off commands

Run any CLI inside the stack's network with `docker compose run --rm`:

```bash
docker compose run --rm api market backfill --as-of latest        # one-off history load (see `market backfill --help`)
docker compose run --rm api market request-engine status
docker compose run --rm api market request-engine resume          # clear a halt after fixing the API key or plan
docker compose run --rm api spend reattribute <session-id> 49     # or `overhead`
docker compose run --rm api spend --help
```

### Dev loop on the host

Develop on the host against the Compose Postgres on `localhost:5433` (`docker compose up -d postgres`); the default database URLs point there.

```bash
cd backend && uv run spend migrate && uv run market migrate     # create + migrate both databases
cd backend && uv run uvicorn hundred_acre.main:app --reload     # http://localhost:8000, health at /api/health
cd frontend && npm install && npm start                         # http://localhost:5173, proxies /api (set API_URL to change)
cd backend && uv run pytest                                     # throwaway databases on the Compose Postgres
cd frontend && npm test                                         # Vitest
```

Stop the containerised `api` first if you want port 8000 for the dev server, or just use the web app on 8100. If your `POSTGRES_PASSWORD` is not the default, export `SPEND_DATABASE_URL`, `SPEND_TEST_SERVER_URL`, `MARKET_DATABASE_URL` and `MARKET_TEST_SERVER_URL` accordingly.

The opt-in smoke test builds and starts a whole throwaway stack (own project name, port and data directory), checks it end to end and tears it down. It is excluded from the default run:

```bash
cd backend && uv run pytest -m compose
```

Spend tests use the `spend_engine` / `spend_engine_factory` fixtures and market tests the `market_engine` and `lake` fixtures: throwaway, migrated databases dropped after each test.

### Restoring a Spend backup

The scheduler's nightly backup and its failure handling are described in [ops/backup/README.md](ops/backup/README.md), which also has the restore steps against the Compose Postgres.

### One-time step for the user: disable the old host backup timer

If the host `hundred-acre-spend-backup.timer` is installed, disable it once the scheduler's backup has succeeded (check `docker compose exec scheduler cat /var/lib/hundred-acre-backup/last-success`), so two backups do not race:

```bash
systemctl --user disable --now hundred-acre-spend-backup.timer
```

The repo no longer ships any unit files; delete any old copies you installed, and stop any host-run Request Engine (the container is the only engine allowed).

## Spend migrations

The Spend database has its own Alembic history in `backend/src/hundred_acre/spend/migrations`, separate from any future main-database migrations.

```bash
docker compose run --rm api spend migrate   # or `cd backend && uv run spend migrate` on the host
```

`spend migrate` and `market migrate` create their database if it is missing, then upgrade to head; a second run is a no-op. `docker compose up` does this for you.

## Spend CLI

```bash
cd backend && uv run spend --help
cd backend && uv run spend record ~/.claude/projects/<project>/<session-id>.jsonl   # also records its subagents/ transcripts
```

API: `GET /api/spend/total` returns `{"dollars": "<full precision>", "unpriced_messages": N}`. The Price Schedule (`price_schedule` table, $ per million units) is seeded by migration 0002 from Anthropic's pricing page.

`GET /api/spend/features` returns per-Feature Spend (sub-issues rolled up) and Overhead. Sessions are attributed from the issue references `record` finds in transcripts (stored in `attributions.refs`, never conversation text) and resolved against GitHub live at query time; if `gh` cannot reach GitHub the response has `github_available: false`. Set `HUNDRED_ACRE_GITHUB_REPO=owner/name` if the API server does not run inside the repo (Compose sets it for the container).

## Automatic recording

`.claude/settings.json` registers a `Stop` (turn end) and `SessionEnd` hook that runs `spend hook` in the background; it always exits 0 immediately. Failed recordings are queued in `~/.local/state/hundred-acre/spend-retry-queue` (override with `SPEND_QUEUE_FILE`) and retried on the next run. To recover gaps or backfill history (including git worktrees): `cd backend && uv run spend sweep`.

## Backups

Nightly offsite backup of `hundred_acre_spend` (run by the `scheduler` service; failure visibility and restore steps): see [ops/backup/README.md](ops/backup/README.md).

Each Feature's Spend is split into Build and Maintenance Spend per message: a message timestamped at or before the top-level issue's first-closed date is Build, a later one is Maintenance (a Session that straddles the close is split). Reopening and re-closing never moves the date. Each Feature also carries `build_dollars`, `maintenance_dollars` and `completed_at` (the first-closed date, null while never closed), which the cumulative chart draws as completion markers; with GitHub unreachable the chart simply has none.

## Market data (Postgres + DuckLake, ADR-0002)

The `postgres` service hosts the Market database (`hundred_acre_market`, role `hundred_acre`, `localhost:5433` from the host); `migrate` keeps it migrated (history in `backend/src/hundred_acre/market/migrations`; manually: `docker compose run --rm api market migrate`). On the host the defaults below apply; inside Compose the URLs point at `postgres:5432`.

| Env var | Default |
| --- | --- |
| `MARKET_DATABASE_URL` | `postgresql+psycopg://hundred_acre:hundred_acre@localhost:5433/hundred_acre_market` (also the DuckLake catalog) |
| `MARKET_TEST_SERVER_URL` | `postgresql+psycopg://hundred_acre:hundred_acre@localhost:5433/postgres` (creates/drops throwaway test DBs) |
| `SPEND_DATABASE_URL` | `postgresql+psycopg://hundred_acre:hundred_acre@localhost:5433/hundred_acre_spend` (Spend, ADR-0001; same server, separate database) |
| `SPEND_TEST_SERVER_URL` | `postgresql+psycopg://hundred_acre:hundred_acre@localhost:5433/postgres` (creates/drops throwaway Spend test DBs) |
| `MARKET_DATA_PATH` | none; directory for the DuckLake Parquet files (used by `Lake.from_env()`) |

Tests use the `market_engine` and `lake` fixtures: a throwaway migrated database and a temp DuckLake directory. DuckDB's `ducklake` and `postgres` extensions are installed on first use (needs network once).

## Request Engine (ADR-0003)

One long-running Request Engine is the only process that calls Massive; all jobs enqueue Requests in Postgres and wait. Everything is configured by environment variables:

| Env var | Default |
| --- | --- |
| `MASSIVE_API_KEY` | none (required; never written to rows, files, logs or errors) |
| `MASSIVE_CALLS_PER_MINUTE` | `5` |
| `MASSIVE_MAX_CONCURRENT_CALLS` | `1` |
| `MARKET_RESPONSES_DIR` | none (required; directory for gzip-compressed response bodies, may point at object storage later) |
| `MARKET_DATABASE_URL` | `postgresql+psycopg://hundred_acre:hundred_acre@localhost:5433/hundred_acre_market` |

The `request-engine` container runs `market request-engine run` and restarts unless stopped (`resume` clears a halt after a 401/403, see One-off commands). Only one engine can run at a time, so do not also start one by hand on the host.

Response bodies older than 90 days are deleted hourly by the engine and their location is cleared on the Call rows; Request and Call rows are kept forever. The responses directory is not backed up: `ops/backup/parquet-sync.exclude` lists it for the offsite Parquet sync (pass it as `--exclude-from`).
