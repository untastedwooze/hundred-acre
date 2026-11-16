import { useEffect, useState } from 'react'

type Feature = {
  issue: number
  title: string
  state: string
  dollars: string
  sessions: number
  low_confidence_sessions: number
}
type Report = {
  github_available: boolean
  features: Feature[]
  overhead_dollars: string
  unresolved_dollars: string
  unpriced_messages: number
}
type State = { status: 'loading' } | { status: 'unavailable' } | { status: 'ok'; report: Report }

const money = (dollars: string) => `$${Number(dollars).toFixed(2)}`

export default function FeatureTable() {
  const [state, setState] = useState<State>({ status: 'loading' })
  const [descending, setDescending] = useState(true)

  useEffect(() => {
    fetch('/api/spend/features')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('bad response'))))
      .then((report: Report) => setState({ status: 'ok', report }))
      .catch(() => setState({ status: 'unavailable' }))
  }, [])

  if (state.status === 'loading') return <p>Loading Feature Spend...</p>
  if (state.status === 'unavailable') return <p>Feature Spend unavailable</p>

  const { report } = state
  if (!report.github_available) {
    return (
      <p>
        Feature Spend unavailable: GitHub is unreachable ({money(report.unresolved_dollars)} of Spend is waiting to be
        attributed).
      </p>
    )
  }

  const sorted = [...report.features].sort((a, b) => (Number(a.dollars) - Number(b.dollars)) * (descending ? -1 : 1))
  return (
    <section>
      {report.unpriced_messages > 0 && <p>Warning: {report.unpriced_messages} unpriced messages are not included.</p>}
      <table>
        <thead>
          <tr>
            <th>Feature</th>
            <th>State</th>
            <th aria-sort={descending ? 'descending' : 'ascending'}>
              <button onClick={() => setDescending(!descending)}>Spend {descending ? '▼' : '▲'}</button>
            </th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((f) => (
            <tr key={f.issue}>
              <td>{`#${f.issue} ${f.title}`}</td>
              <td>{f.state}</td>
              <td>{money(f.dollars)}</td>
            </tr>
          ))}
          <tr>
            <td>Overhead</td>
            <td></td>
            <td>{money(report.overhead_dollars)}</td>
          </tr>
        </tbody>
      </table>
    </section>
  )
}
