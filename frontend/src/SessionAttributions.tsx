import { useCallback, useEffect, useState } from 'react'
import { formatDollars, formatLocal } from './spend/format'

type Session = {
  session_id: string
  dollars: string
  last_seen: string
  source: 'inferred' | 'manual'
  target_issue: number | null
  target_title: string | null
  low_confidence: boolean
}
type Option = { issue: number; title: string }
type State = { status: 'loading' } | { status: 'unavailable' } | { status: 'ok'; sessions: Session[] }

/** Sessions with their Attribution, a needs-review filter, and a re-attribute control per row. */
export default function SessionAttributions() {
  const [state, setState] = useState<State>({ status: 'loading' })
  const [needsReview, setNeedsReview] = useState(false)
  const [options, setOptions] = useState<Option[]>([])
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    fetch(`/api/spend/sessions${needsReview ? '?needs_review=true' : ''}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('bad response'))))
      .then((body: { github_available: boolean; sessions: Session[] }) =>
        setState(body.github_available ? { status: 'ok', sessions: body.sessions } : { status: 'unavailable' }),
      )
      .catch(() => setState({ status: 'unavailable' }))
  }, [needsReview])

  useEffect(load, [load])

  useEffect(() => {
    fetch('/api/spend/features')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('bad response'))))
      .then((body: { features: Option[] }) => setOptions(body.features.map(({ issue, title }) => ({ issue, title }))))
      .catch(() => setOptions([]))
  }, [])

  async function reattribute(sessionId: string, choice: string) {
    if (!choice) return
    setError(null)
    try {
      const response = await fetch(`/api/spend/sessions/${sessionId}/attribution`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target_issue: choice === 'overhead' ? null : Number(choice) }),
      })
      if (!response.ok) throw new Error('bad response')
      load()
    } catch {
      setError(`Could not re-attribute session ${sessionId}.`)
    }
  }

  return (
    <section aria-labelledby="sessions-heading">
      <h2 id="sessions-heading">Sessions</h2>
      <label>
        <input type="checkbox" checked={needsReview} onChange={(e) => setNeedsReview(e.target.checked)} /> Needs review only
      </label>
      {error && <p role="alert">{error}</p>}
      {state.status === 'loading' && <p>Loading Sessions...</p>}
      {state.status === 'unavailable' && <p>Sessions unavailable</p>}
      {state.status === 'ok' && state.sessions.length === 0 && (
        <p>{needsReview ? 'Nothing needs review.' : 'No Sessions recorded yet.'}</p>
      )}
      {state.status === 'ok' && state.sessions.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Session</th>
              <th>Last active</th>
              <th>Spend</th>
              <th>Feature</th>
              <th>Source</th>
              <th>Re-attribute</th>
            </tr>
          </thead>
          <tbody>
            {state.sessions.map((s) => (
              <tr key={s.session_id}>
                <td>{s.session_id}</td>
                <td>
                  <time dateTime={s.last_seen}>{formatLocal(s.last_seen)}</time>
                </td>
                <td>{formatDollars(s.dollars)}</td>
                <td>
                  {s.target_issue === null ? 'Overhead' : `#${s.target_issue} ${s.target_title}`}
                  {s.low_confidence && <strong> (needs review)</strong>}
                </td>
                <td>{s.source}</td>
                <td>
                  <select
                    aria-label={`Re-attribute ${s.session_id}`}
                    value=""
                    onChange={(e) => reattribute(s.session_id, e.target.value)}
                  >
                    <option value="">Choose...</option>
                    {options.map((o) => (
                      <option key={o.issue} value={o.issue}>{`#${o.issue} ${o.title}`}</option>
                    ))}
                    <option value="overhead">Overhead</option>
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}
