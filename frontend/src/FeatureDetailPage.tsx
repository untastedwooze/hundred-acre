import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { formatDollars, formatLocal } from './spend/format'

type Amount = { dollars: string }
type Detail = {
  github_available: boolean
  issue: number
  title: string
  state: string
  dollars: string
  unpriced_messages: number
  by_stage: (Amount & { stage: string })[]
  by_sub_issue: (Amount & { issue: number; title: string })[]
  by_model: (Amount & { model: string })[]
  by_session: (Amount & { session_id: string; stage: string; sub_issue: number; first_seen: string })[]
}
type State =
  | { status: 'loading' }
  | { status: 'unavailable' }
  | { status: 'not-found' }
  | { status: 'github-down' }
  | { status: 'ok'; detail: Detail }

function Breakdown({ id, title, headers, rows }: { id: string; title: string; headers: string[]; rows: string[][] }) {
  return (
    <section aria-labelledby={id}>
      <h2 id={id}>{title}</h2>
      <table>
        <thead>
          <tr>
            {headers.map((h) => (
              <th key={h}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((cells) => (
            <tr key={cells[0]}>
              {cells.map((c, i) => (
                <td key={i}>{c}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

/** A Feature's Spend broken down by Stage, sub-issue, model and Session. */
export default function FeatureDetailPage() {
  const { issue } = useParams()
  const [state, setState] = useState<State>({ status: 'loading' })

  useEffect(() => {
    fetch(`/api/spend/features/${issue}`)
      .then((r) => {
        if (r.status === 404) return { status: 'not-found' } as State
        if (!r.ok) return Promise.reject(new Error('bad response'))
        return r.json().then((d: Detail) => (d.github_available ? { status: 'ok', detail: d } : { status: 'github-down' }) as State)
      })
      .then(setState)
      .catch(() => setState({ status: 'unavailable' }))
  }, [issue])

  const back = <Link to="/spend">Back to Spend</Link>
  if (state.status === 'loading') return <p>Loading Feature Spend...</p>
  if (state.status === 'unavailable') return <><p>Feature breakdown unavailable</p>{back}</>
  if (state.status === 'not-found') return <><p>Feature not found</p>{back}</>
  if (state.status === 'github-down') return <><p>Feature breakdown unavailable: GitHub is unreachable.</p>{back}</>

  const d = state.detail
  return (
    <>
      {back}
      <h1>{`#${d.issue} ${d.title}`}</h1>
      <p>
        <strong>{formatDollars(d.dollars)}</strong> ({d.state})
      </p>
      {d.unpriced_messages > 0 && <p role="alert">Warning: {d.unpriced_messages} unpriced messages are not included.</p>}
      <Breakdown
        id="by-stage"
        title="By Stage"
        headers={['Stage', 'Spend']}
        rows={d.by_stage.map((s) => [s.stage, formatDollars(s.dollars)])}
      />
      <Breakdown
        id="by-sub-issue"
        title="By sub-issue"
        headers={['Issue', 'Spend']}
        rows={d.by_sub_issue.map((s) => [`#${s.issue} ${s.title}`, formatDollars(s.dollars)])}
      />
      <Breakdown
        id="by-model"
        title="By model"
        headers={['Model', 'Spend']}
        rows={d.by_model.map((m) => [m.model, formatDollars(m.dollars)])}
      />
      <Breakdown
        id="by-session"
        title="By Session"
        headers={['Session', 'Stage', 'Sub-issue', 'Started', 'Spend']}
        rows={d.by_session.map((s) => [s.session_id, s.stage, `#${s.sub_issue}`, formatLocal(s.first_seen), formatDollars(s.dollars)])}
      />
    </>
  )
}
