# Bars and derived analytics live in DuckLake; everything else lives in Postgres

Bars (Daily and Intraday) and the analytics derived from them are stored as Parquet files in a DuckLake whose catalog is kept in Postgres, and are read with DuckDB or Polars. Relational data (Watchlist, Corporate Actions, security metadata, Massive request engine state, users, portfolios) lives in ordinary Postgres tables. The dominant workload is pulling many securities over a time window into a DataFrame for covariance and volatility estimation. Columnar Parquet serves that roughly 5–20× faster than row-by-row transfer out of Postgres, and the DuckLake catalog adds transactional appends, corrections and snapshots on top of the files.

## Considered Options

- **Postgres with native partitioning**: no compression, and bulk reads into Python are bottlenecked by row transfer.
- **Postgres with TimescaleDB**: compresses old data well, but has the same bulk-read bottleneck. It would also need its own Postgres image, and hosting it would need a large managed Postgres.
- **Plain Parquet without DuckLake**: just as fast to read, but loads that fail partway and corrections would require hand-managed file rewrites with no transactions.

## Consequences

Derived analytics are a recomputable cache, never a source of truth. Each derived row records the Bar snapshot it was computed from, so corrections to Bars show which results are stale.
