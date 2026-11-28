# The Request Engine clamps Bar windows to the History Limit at Call time

Massive serves Bars only back to the History Limit, a fixed span of years before today. The span is configured (`MASSIVE_HISTORY_YEARS`, default 2 for the free plan) and the History Limit is computed from today's **UTC** date, inclusive. Our Calls show the boundary day is served; whether Massive's day turns at midnight ET or UTC is unknown, and the UTC date is never behind New York's, so it is the safe choice at the cost of one day for a few evening hours.

When the Request Engine makes a Call to a date-ranged Bar endpoint (per-Ticker Daily and Intraday aggregates, grouped daily), it moves the window's start up to the History Limit as of that Call and records the window it actually sent on the Call row. A Request whose whole window is before the History Limit completes with an empty result and no Call. Jobs keep asking for their natural window, and backfill's default start is the History Limit itself, so a plan upgrade only needs the setting changed.

Coverage stores nothing for days before the History Limit. Coverage queries count only days on or after it, so those days are neither fetched nor reported missing, and rows for days that have since fallen behind it are kept but ignored.

Reference endpoints (splits, dividends, tickers, ticker events, market status) are not clamped. Any 401/403 still halts the Request Engine (ADR 0003), and for Bar endpoints the halt reason names the window sent and the History Limit applied.

## Considered Options

- **Each job picks a start inside the limit** (`backfill --since`, #73): rejected as the fix because every job that reaches back needs its own workaround. `--since` stays as an ordinary "start later" option.
- **Clamp when the Request is queued** (in `EngineSource` or the request builders): rejected because backfill-class Requests can wait days in the queue and drift outside the limit before they are served.
- **Learn the limit from Massive**: rejected because there is no endpoint for it, and probing for the boundary spends Calls at 5 a minute.
- **A stored Coverage status for days before the limit**: rejected because the limit moves daily, which would mean writing rows for every Security every day, and those rows would have to be cleaned up after a plan upgrade.
- **Clamp the reference endpoints too**: rejected because nobody knows whether they are limited, and silently shortening corporate-action history would corrupt Adjustment Factors without any sign. A halt is the honest failure.
- **Fail only the Request on a Bar-endpoint 403 instead of halting**: rejected because after clamping such a 403 means a wrong span or revoked access, and failing Requests one by one marked 6,672 Securities failed on 2026-11-27 before anyone noticed.

## Consequences

- A Request's stored parameters can differ from the Call that answered it. The Call row is the record of what was sent.
- A wrongly configured span still halts the Engine on the first out-of-reach Call, which is loud but cheap.
- The free plan also refuses the current trading day until after midnight ET. That is not covered here, and a job asking for it would halt the Engine.
