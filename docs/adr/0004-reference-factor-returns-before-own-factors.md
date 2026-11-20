# Import published Factor Returns first; build our own Factors later and validate them against the published ones

Factor work starts by importing Reference Factor Returns (daily Fama-French five factors, momentum and the risk-free rate from Kenneth French's Data Library) and using them to estimate per-Security Factor Exposures. Building Factors ourselves from EDGAR fundamentals comes later, as separate Features. Their acceptance test is reproducing the Reference Factor Returns closely over the period where both exist. Attribution needs only Factor Returns, which French already publishes for free. The hard part of building Factors ourselves is point-in-time fundamentals (filing lags, restatements, fiscal-year alignment), and that has no objective test of correctness unless a trusted reference exists to compare against.

## Considered Options

- **Build Factors from EDGAR first**: rejected because nothing would be usable until the whole fundamentals pipeline worked, and there would be no independent check that it was right.
- **Use only the published Factor Returns, never build our own**: rejected because per-Security characteristics, Factors on our own Universe and new characteristics all need fundamentals.
- **A paid fundamentals vendor instead of EDGAR**: deferred until the EDGAR Feature is specced. EDGAR is free and already reachable through each Issuer's CIK.

## Consequences

- Reference Factor Returns are stored as vintaged snapshots alongside Bars, because French restates history. Each Factor Exposure records the vintage it used, so a restatement marks the affected exposures as stale.
- Factor Returns lag publication by one to two months, so the most recent estimation windows stay uncomputed until the next import.
- French's site is not Massive, so imports do not go through the Request Engine.
