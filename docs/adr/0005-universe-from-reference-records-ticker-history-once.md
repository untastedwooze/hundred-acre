# The Universe is synced from Reference Records nightly, and Ticker History is fetched once per Security

The reference sync builds Ticker periods from Reference Records alone: Massive's active and inactive ticker lists, requested with `market=stocks`. An inactive record ends on its `delisted_utc`. The same Security's next record (ordered by delisting date, active last) starts the following day. The first record starts the day after the previous holder of that Ticker was delisted, if there was one. Otherwise it starts on the date Ticker History gives, or is open-ended (NULL) if there is no History. Listing all records is ~38 Calls, so the sync runs once at the top of `nightly`, `weekly` and `backfill`, in a single transaction. There is no separate cron entry. `market reference-sync` stays as a manual command.

Ticker History (the events endpoint, one Call per Security) is stored in its own table. It is fetched once per Security, and again only when that Security's set of Reference Records `(ticker, active, delisted_utc)` changes, or when a manual refetch is asked for. The fetch runs at `backfill` priority, one Security at a time, and can be resumed. A 404 means the Security has no History. Where the two sources disagree, the Reference Records win. A Reference Record supplies its Ticker and its end. Ticker History supplies only the start date (the latest event naming that Ticker) and any earlier Tickers, each running until the next event. Events dated after that start are logged and ignored, though they stay in the stored History. Sometimes several inactive records for one Security share a delisting date (`RG`/`RGw`, `JBT`/`JBTw`). Then only the main Ticker gets a period: the one without a `w`, `.Q` or `E`-style suffix, or else the first alphabetically. Bars under the other Tickers become Quarantined Bars. Daily and Intraday Bars gain a `ticker` column, for provenance only. Bars are still one per Security per day (or per five-minute interval).

## Considered Options

- **Fetch every Ticker's events on every sync** (the original `sync_reference`): rejected because each run makes ~16,000 Calls, about 54 hours at 5 Calls/min. Also, 92% of responses hold a single event, which adds nothing to the lists.
- **Rotate a 1/30 slice of Tickers through the events fetch each night**: rejected because it still spends ~540 Calls a night, mostly on unchanged data. A rename could go a month unseen, while the lists already show it the next day.
- **Fetch Ticker History lazily, only when Quarantined Bars appear**: rejected because a missing earlier Ticker doesn't produce Quarantined Bars. It just leaves history silently short.
- **Treat Ticker History as authoritative**: rejected because it contradicts the lists in ~10% of multi-event responses (blank Tickers, placeholders like `S393`, suffixed variants like `AEISE`).
- **Reuse stored responses in the Request Engine**: rejected, keeping ADR 0003. The ~5,568 events responses already on disk are imported once by a throwaway script, not through the engine.

## Consequences

- The first backfill spends ~35 hours of budget on Ticker History before Daily Bars. After that, History Calls happen only when a Security's Reference Records change.
- A rename that the lists don't show (~4.7% of events responses name a former Ticker that no Reference Record holds) is covered only by Ticker History. If History is never refetched, a later such rename is missed.
- Adding an unknown Ticker to the Watchlist makes one lookup Call across all markets instead of a full sync. If the Security is new, its Ticker History fetch is queued.
- Securities without a share-class FIGI (18,190 inactive stock records) still get no Security, so the Universe keeps a survivorship gap. That is tracked separately.
