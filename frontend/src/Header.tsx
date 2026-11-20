import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

type Total = { dollars: string; unpriced_messages: number }
type State = { status: 'loading' } | { status: 'unavailable' } | { status: 'ok'; total: Total }

function formatDollars(dollars: string): string {
  return `$${Number(dollars).toFixed(2)}`
}

export default function Header() {
  const [state, setState] = useState<State>({ status: 'loading' })

  useEffect(() => {
    fetch('/api/spend/total')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('bad response'))))
      .then((total: Total) => setState({ status: 'ok', total }))
      .catch(() => setState({ status: 'unavailable' }))
  }, [])

  return (
    <header>
      <strong>hundred-acre</strong>
      <nav aria-label="Main">
        <Link to="/factors">Factor Exposures</Link>
      </nav>
      <span aria-label="Total Spend">
        {state.status === 'ok' && (
          <>
            <Link to="/spend">
              Spend to date: <span>{formatDollars(state.total.dollars)}</span>
            </Link>
            {state.total.unpriced_messages > 0 && <em> ({state.total.unpriced_messages} unpriced)</em>}
          </>
        )}
        {state.status === 'unavailable' && 'Spend unavailable'}
      </span>
    </header>
  )
}
