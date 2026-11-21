# hundred-acre
A fullstack, vibe-coded, personal investing application and backtester where the cost to implement every feature is transparent in the web interface.

## Run with Docker Compose

```bash
cp .env.example .env        # fill in GH_TOKEN_READ (Issues read on hundred-acre only); optional, see below
docker compose up -d        # postgres -> migrate (creates and migrates both databases) -> api -> web
```

The app is at `http://localhost:8100` (`WEB_BIND` / `WEB_PORT` change that; `WEB_BIND=0.0.0.0` opens it to your LAN, and there is no authentication). Postgres is published only on `127.0.0.1:5433` with its password from `POSTGRES_PASSWORD` (it applies when the `hundred_acre_pg` volume is first created). Without `GH_TOKEN_READ` the Spend views report GitHub unavailable.

One-off commands: `docker compose run --rm migrate spend --help`.

Opt-in smoke test of the whole stack (builds images, throwaway project name and ports): `cd backend && uv run pytest -m compose`.

## Prerequisites (host-side dev)

- [uv](https://docs.astral.sh/uv/), Node 20+, and the Compose Postgres running on `localhost:5433` (`docker compose up -d postgres`).

## Postgres setup (Spend database)

The Spend database still lives on `scrooge-mcduck-db-1` until the Spend cutover; hundred-acre uses its own role and database, per ADR-0001:

```bash
docker exec scrooge-mcduck-db-1 psql -U app -d app \
  -c "CREATE ROLE hundred_acre LOGIN PASSWORD 'hundred_acre' CREATEDB" \
  -c "CREATE DATABASE hundred_acre_spend OWNER hundred_acre"
```

(Already done on this machine.) `psql` is not on the host PATH; use `docker exec scrooge-mcduck-db-1 psql -U app -d hundred_acre_spend`.

Connection settings (defaults shown; override with env vars):

| Env var | Default |
| --- | --- |
| `SPEND_DATABASE_URL` | `postgresql+psycopg://hundred_acre:hundred_acre@localhost:5432/hundred_acre_spend` |
| `SPEND_TEST_SERVER_URL` | `postgresql+psycopg://hundred_acre:hundred_acre@localhost:5432/postgres` (server used to create/drop throwaway test DBs) |

## Run

```bash
# Backend (http://localhost:8000, health at /api/health)
cd backend && uv run spend migrate && uv run uvicorn hundred_acre.main:app --reload

# Front end (http://localhost:5173, proxies /api to the backend; set API_URL to change)
cd frontend && npm install && npm start
```

## Spend migrations

The Spend database has its own Alembic history in `backend/src/hundred_acre/spend/migrations`, separate from any future main-database migrations.

```bash
cd backend && uv run spend migrate        # upgrade to head
```

## Tests

```bash
cd backend && uv run pytest     # Spend tests use the `spend_engine` / `spend_engine_factory` fixtures:
                                # a throwaway, migrated Postgres database, dropped after each test
cd frontend && npm test         # Vitest
```

## Spend CLI

```bash
cd backend && uv run spend --help
cd backend && uv run spend record ~/.claude/projects/<project>/<session-id>.jsonl   # also records its subagents/ transcripts
```

API: `GET /api/spend/total` returns `{"dollars": "<full precision>", "unpriced_messages": N}`. The Price Schedule (`price_schedule` table, $ per million units) is seeded by migration 0002 from Anthropic's pricing page.

`GET /api/spend/features` returns per-Feature Spend (sub-issues rolled up) and Overhead. Sessions are attributed from the issue references `record` finds in transcripts (stored in `attributions.refs`, never conversation text) and resolved against GitHub live at query time; if `gh` cannot reach GitHub the response has `github_available: false`. Set `HUNDRED_ACRE_GITHUB_REPO=owner/name` if the API server does not run inside the repo.

## Automatic recording

`.claude/settings.json` registers a `Stop` (turn end) and `SessionEnd` hook that runs `spend hook` in the background; it always exits 0 immediately. Failed recordings are queued in `~/.local/state/hundred-acre/spend-retry-queue` (override with `SPEND_QUEUE_FILE`) and retried on the next run. To recover gaps or backfill history (including git worktrees): `cd backend && uv run spend sweep`.

## Backups

Nightly offsite backup of `hundred_acre_spend` (systemd user timer, restore steps): see [ops/backup/README.md](ops/backup/README.md).

Each Feature's Spend is split into Build and Maintenance Spend per message: a message timestamped at or before the top-level issue's first-closed date is Build, a later one is Maintenance (a Session that straddles the close is split). Reopening and re-closing never moves the date. Each Feature also carries `build_dollars`, `maintenance_dollars` and `completed_at` (the first-closed date, null while never closed), which the cumulative chart draws as completion markers; with GitHub unreachable the chart simply has none.

## Market data (Postgres + DuckLake, ADR-0002)

`docker compose up -d` runs a dedicated hundred-acre Postgres (`localhost:5433`, role `hundred_acre`, database `hundred_acre_market`). Migrate with Alembic (history in `backend/src/hundred_acre/market/migrations`):

```bash
cd backend && uv run python -c "from hundred_acre.market.db import migrate; migrate()"
```

| Env var | Default |
| --- | --- |
| `MARKET_DATABASE_URL` | `postgresql+psycopg://hundred_acre:hundred_acre@localhost:5433/hundred_acre_market` (also the DuckLake catalog) |
| `MARKET_TEST_SERVER_URL` | `postgresql+psycopg://hundred_acre:hundred_acre@localhost:5433/postgres` (creates/drops throwaway test DBs) |
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

Run by hand with `cd backend && uv run market request-engine run` (`--once` serves until the queue is empty and exits; `resume` clears a halt after a 401/403). Only one engine can run at a time.

Response bodies older than 90 days are deleted hourly by the engine and their location is cleared on the Call rows; Request and Call rows are kept forever. The responses directory is not backed up: `ops/backup/parquet-sync.exclude` lists it for the offsite Parquet sync (pass it as `--exclude-from`).

### Install as a systemd user service

```bash
mkdir -p ~/.config/hundred-acre ~/.config/systemd/user
cat > ~/.config/hundred-acre/request-engine.env <<'ENV'
MASSIVE_API_KEY=...
MARKET_RESPONSES_DIR=/path/to/responses
# optional: MASSIVE_CALLS_PER_MINUTE, MASSIVE_MAX_CONCURRENT_CALLS, MARKET_DATABASE_URL
ENV
chmod 600 ~/.config/hundred-acre/request-engine.env
cp ops/request-engine/hundred-acre-request-engine.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hundred-acre-request-engine.service
loginctl enable-linger "$USER"   # optional: run even when not logged in
```

The unit uses `Restart=always`; the repo is assumed at `~/honey-tree/hundred-acre` and `uv` at `~/.local/bin/uv` (edit the unit otherwise). This is a stopgap: the app is meant to move fully to Docker Compose, and since all configuration is environment variables, that needs only a backend image and a service entry.
