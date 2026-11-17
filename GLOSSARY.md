# hundred-acre

A personal investment portfolio backtester whose own development spend on Claude is tracked and shown as a first-class part of the product.

## Language

### Market data

**Universe**:
Every exchange-listed Security, plus any Security added to the Watchlist by hand. Over-the-counter Securities are outside the Universe unless added this way.
_Avoid_: Market, all tickers

**Security**:
One tradable share class with a continuous price history, whatever Tickers it has traded under. A renamed Ticker stays the same Security; a reused Ticker belongs to a new Security. Each Security has a type, such as common stock, ETF, ADR, preferred, warrant, unit or right.
_Avoid_: Ticker, symbol, stock, instrument

**Issuer**:
The company or fund that issues one or more Securities, such as Alphabet issuing both GOOGL and GOOG.
_Avoid_: Company, entity, CIK

**Ticker**:
The symbol a Security trades under during a given period. A Security can have several Tickers over its life, and a Ticker can belong to different Securities at different times.
_Avoid_: Symbol, security

**Provisional Security**:
A Security created from a Ticker that had no matching reference record for several trading days, marked unverified until a FIGI is attached to it.
_Avoid_: Unknown ticker, placeholder

**Quarantined Bar**:
A Bar whose Ticker could not yet be matched to any Security on its date, held apart from all other Bars until it is.
_Avoid_: Orphan, unmapped row

**Bar**:
A summary of one Security's trading over a fixed interval: open, high, low, close, volume and volume-weighted average price. Bars are always stored Unadjusted.
_Avoid_: Candle, tick, quote

**Daily Bar**:
A Bar covering one trading day.
_Avoid_: EOD price, close

**Intraday Bar**:
A Bar covering five minutes of one trading day.
_Avoid_: Minute bar, candle

**Trading Session**:
The part of a trading day a Bar falls in: overnight, pre-market, regular or after-hours. Regular hours are 9:30–16:00 ET except on half days; overnight trading belongs to the trading day it leads into.
_Avoid_: Session (that means a Claude Code conversation), market hours

**Trading Calendar**:
The record of which days a market is open and its open and close times on each, used to decide which Bars should exist.
_Avoid_: Holiday list, schedule

**Unadjusted**:
Prices and volumes exactly as they traded on the day, never restated for later splits or dividends. Adjustments are computed from Corporate Actions when data is read, so history never needs rewriting.
_Avoid_: Raw (ambiguous), as-traded

**Corporate Action**:
A dated event that changes how a Security's past prices compare with its future ones, or what its holders own: a split, a dividend, a spinoff or a Delisting.
_Avoid_: Adjustment, event

**Delisting**:
The Corporate Action that ends a Security's trading, with a terminal outcome for its holders: cash per share, conversion into another Security at a ratio, or worthless. An outcome not confirmed by hand is assumed to be cash at the last Daily Bar's close.
_Avoid_: Delisting return, termination

**Coverage**:
The record of which Bars have been fetched for each Security, bar interval and trading day, and whether each fetch was complete, empty, failed, partial or suspect (disagreeing with that day's Daily Bar).
_Avoid_: Load log, inventory

**Adjustment Factor**:
A Security's cumulative multiplier for a given day, derived from its Corporate Actions, that turns Unadjusted prices into comparable ones. The split factor covers splits only; the total-return factor also reinvests dividends.
_Avoid_: Adjusted price, multiplier

**Daily Security Statistics**:
Per-Security, per-trading-day figures derived from Bars: realized volatility from regular-hours Intraday Bars, overnight return, dollar volume and average volume. Like all derived analytics, they can always be recomputed from Bars and Corporate Actions.
_Avoid_: Metrics, indicators, features

**Watchlist**:
The curated set of Securities whose Intraday Bars are refreshed nightly. All other Securities in the Universe have their Intraday Bars refreshed weekly.
_Avoid_: Curated list, portfolio

### Development spend

**Spend**:
The API-equivalent dollar cost of Claude usage: usage priced by the Price Schedule in effect at the time each message was made. It is an estimate of value consumed, not an amount billed.
_Avoid_: Credits, bill, charge, actual cost

**Price Schedule**:
The dated record of Anthropic's list prices for each model, token type and pricing modifier (cache lifetime, long context, server tools, fast mode), used to turn token counts into Spend. Usage for a model not listed in it is **unpriced**: counted in tokens but flagged as having no dollar value.
_Avoid_: Rate card, pricing

**Usage Ledger**:
The permanent, append-only record of every Claude message's model and token counts across the project's development history. It is the single source of truth for Spend.
_Avoid_: Cost log, transcript

**Session**:
One Claude Code conversation, including any subagents it spawns. It is the unit of attribution: each Session's Spend belongs to exactly one Feature or to Overhead.
_Avoid_: Conversation, chat, run

**Feature**:
A unit of product work defined by one top-level GitHub issue (an issue with no parent), together with all of its descendant sub-issues. A Feature's Spend is the total Spend of all work attributed to it or any of its sub-issues, including bugs later filed against it.
_Avoid_: Ticket, task, story, epic

**Build Spend**:
A Feature's Spend incurred before its top-level issue was first closed.
_Avoid_: Initial cost, dev cost

**Maintenance Spend**:
A Feature's Spend incurred after its top-level issue was first closed, typically from bugs filed as its sub-issues.
_Avoid_: Bug cost, upkeep

**UI Surface**:
A named part of the web interface built by a Feature, declared on that Feature's issue, where its Spend is displayed.
_Avoid_: Page, screen, component

**Attribution**:
The assignment of a Session to one Feature or to Overhead. An Attribution is either inferred from the Session's activity or set manually, and a manual Attribution always overrides an inferred one.
_Avoid_: Tag, label, allocation

**Stage**:
The step of the development pipeline a Session performs: grill, spec, implement, review, fix, or other. It is inferred from the first pipeline skill the Session runs.
_Avoid_: Phase, step

**Overhead**:
Spend that is not attributed to any Feature, such as setup, triage, or cross-cutting maintenance. It is always counted in total Spend.
_Avoid_: Unattributed, misc, other
