# hundred-acre

A personal investment portfolio backtester whose own development spend on Claude is tracked and shown as a first-class part of the product.

## Language

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
