# Spend data lives in its own Postgres database; Features live in GitHub

The Usage Ledger, Attributions and Price Schedule are stored in a dedicated `hundred_acre_spend` database on the project's local Postgres server, isolated from the main product database and backed up offsite on a schedule. Feature metadata (titles, sub-issue tree, status, UI surface) is read live from GitHub issues, which are already its source of truth. If GitHub is unreachable, Spend views degrade but the rest of the app keeps working.

## Considered Options

- **Committed files in the repo** (ledger, prices, Feature catalog): rejected as a pseudo-database maintained in code.
- **Structured comments on GitHub issues**: rejected because agents also comment on issues, re-attribution would mean moving comments between issues, schema changes would mean rewriting history, and every query would need to fetch and parse all comments.
- **Hosted Postgres (Neon) for Spend**: rejected as extra infrastructure once a local Postgres server was already required for the main database.
- **A schema inside the main database**: rejected because restoring or migrating the large main database would then touch Spend data.

## Consequences

Spend history can't be rebuilt once Claude Code transcripts expire, so the offsite backup of `hundred_acre_spend` matters more than the backup of the main database.
