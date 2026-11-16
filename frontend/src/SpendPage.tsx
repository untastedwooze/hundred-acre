import { useEffect, useState } from 'react'
import FeatureTable from './FeatureTable'
import CoverageNote from './spend/CoverageNote'
import CumulativeSpendChart, { type FeatureMarker, type SeriesPoint } from './spend/CumulativeSpendChart'
import { formatDollars, formatLocal } from './spend/format'
import SpendBadge from './spend/SpendBadge'

type Series = { points: SeriesPoint[]; unpriced_messages: number }
type State = { status: 'loading' } | { status: 'unavailable' } | { status: 'ok'; series: Series }

function CumulativeSection() {
  const [state, setState] = useState<State>({ status: 'loading' })
  const [markers, setMarkers] = useState<FeatureMarker[]>([])

  useEffect(() => {
    // Completion markers are optional: if Feature data (GitHub) is unavailable the chart simply has none.
    fetch('/api/spend/features')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('bad response'))))
      .then((report: { features?: { issue: number; title: string; completed_at: string | null }[] }) =>
        setMarkers((report.features ?? []).filter((f): f is FeatureMarker => f.completed_at !== null)),
      )
      .catch(() => setMarkers([]))
  }, [])

  useEffect(() => {
    fetch('/api/spend/series')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('bad response'))))
      .then((series: Series) => setState({ status: 'ok', series }))
      .catch(() => setState({ status: 'unavailable' }))
  }, [])

  if (state.status === 'loading') return <p>Loading…</p>
  if (state.status === 'unavailable') return <p>Spend unavailable</p>
  const { points, unpriced_messages } = state.series
  const first = points[0]
  const last = points[points.length - 1]
  return (
    <section aria-labelledby="cumulative-heading">
      <h2 id="cumulative-heading">Cumulative Spend</h2>
      {unpriced_messages > 0 && <p role="alert">{unpriced_messages} unpriced messages are not included in this series.</p>}
      {points.length === 0 ? (
        <p>No Spend recorded yet.</p>
      ) : (
        <>
          <p>
            <strong>{formatDollars(last.dollars)}</strong>
          </p>
          <CumulativeSpendChart points={points} markers={markers} />
          <p>
            <time dateTime={first.timestamp}>{formatLocal(first.timestamp)}</time> to{' '}
            <time dateTime={last.timestamp}>{formatLocal(last.timestamp)}</time>
          </p>
        </>
      )}
    </section>
  )
}

/**
 * The Spend page. Each section is self-contained and fetches its own data, so later tickets
 * (Feature table, drill-down, needs-review) add a section here without touching the others.
 */
export default function SpendPage() {
  return (
    <>
      <h1>Spend</h1>
      <SpendBadge surface="spend" />
      <CumulativeSection />
      <h2>Features</h2>
      <FeatureTable />
      <CoverageNote />
    </>
  )
}
