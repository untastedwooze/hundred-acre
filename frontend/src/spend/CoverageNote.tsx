export default function CoverageNote() {
  return (
    <aside role="note" aria-label="Coverage">
      <strong>Coverage.</strong> Spend is an estimate (API-equivalent dollars, not an amount billed) computed from
      local Claude Code Session transcripts in this repo. It excludes Claude Code background calls, cloud sessions and
      claude.ai chats. Times are stored in UTC and shown in your local time.
    </aside>
  )
}
