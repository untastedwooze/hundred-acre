# All Massive access goes through one Request Engine with a durable Postgres queue

Every Massive Call is made by a single long-running Request Engine process. Callers (nightly, weekly and backfill jobs, the CLI, and later the web backend) never call Massive themselves. They enqueue Requests in Postgres and wait for them. Massive's rate limit (5 Calls/min on the free tier) is the scarcest resource in the system, and it has to be shared across processes. An in-process limiter gives each process its own budget, so concurrent jobs exceed the limit together, and a long backfill in one process can't give way to the nightly job in another. The engine serves Requests in strict Priority Class order and computes its sliding window from a persisted Call log, so restarts don't reset the window. It follows pagination cursors itself, retries transient failures, and halts on credential errors. `EngineSource` implements the existing `MarketDataSource` boundary, so loaders don't change.

## Considered Options

- **In-process rate limiter per job** (the original `ThrottledSource`): rejected because concurrent processes each assume the full budget, and priority is only honoured within one process.
- **Shared budget library coordinated through Postgres locks**: rejected because each process still makes its own Calls, so cross-process priority and crash-safe resumption of paged Requests would need the same machinery as a queue anyway.
- **Engine as an HTTP service with an in-memory queue**: rejected because a restart loses queued work and the rate window.
- **Engine runs domain tasks (fetch-and-store Bars) instead of raw Requests**: rejected because it moves loader logic into the engine. Coverage already makes loaders restartable.

## Consequences

- When the Request Engine is stopped or halted, no Massive data flows anywhere. Callers fail fast with `MarketDataUnavailable`, and Coverage marks the work for the next run. A halt is cleared only by hand (`market request-engine resume`).
- Completed Requests are never reused as a cache, so nightly correction re-fetches always reach Massive. Only identical Requests that are pending at the same time are coalesced.
- A Request that no caller is still waiting on is abandoned before its next Call, so a crashed backfill doesn't keep spending the budget.
- Raw response bodies are kept outside Postgres for 90 days and are not backed up. Request and Call metadata are kept in Postgres forever as the audit trail.
