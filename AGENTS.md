# hundred-acre

A fullstack, vibe-coded, personal investing application and backtester where the cost to implement every feature is transparent in the web interface.

## Agent skills

### Issue tracker

Issues and specs are tracked as GitHub issues in `untastedwooze/hundred-acre` via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles map to identically named labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: one `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

### UI Surfaces and Spend badges

Whenever a Feature adds visible UI, the implementing agent must:

1. Declare the UI Surface on the Feature's top-level spec issue with a whole line in the body: `ui-surface: <name>` (lowercase, no spaces, e.g. `ui-surface: spend`). Ask the user to add it if you cannot edit the issue.
2. Place `<SpendBadge surface="<name>" />` (`frontend/src/spend/SpendBadge.tsx`) on that surface. It shows "Built for $X" linking to the Feature's breakdown, and handles the GitHub-unavailable and unknown-surface states. It must be rendered inside a Router.
