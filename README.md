# hundred-acre
A fullstack, vibe-coded, personal investing application and backtester where the cost to implement every feature is transparent in the web interface.

## Prerequisites

- [uv](https://docs.astral.sh/uv/), Node 20+, and a local Postgres server on `localhost:5432`.

## Postgres setup (Spend database)

On this machine Postgres runs in the docker container `scrooge-mcduck-db-1` (postgres:16, superuser `app`, no `postgres` role). hundred-acre uses its own role and database, per ADR-0001:

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
