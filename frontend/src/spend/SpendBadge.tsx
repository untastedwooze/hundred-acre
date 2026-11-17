import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { formatDollars } from './format'

type State =
  | { status: 'loading' }
  | { status: 'unavailable' }
  | { status: 'unknown' }
  | { status: 'ok'; issue: number; dollars: string }

/**
 * "Built for $X" badge for a UI Surface. `surface` is the name declared by a `ui-surface: <name>` line
 * in the Feature's issue body. Links to the Feature's breakdown. Must be rendered inside a Router.
 */
export default function SpendBadge({ surface }: { surface: string }) {
  const [state, setState] = useState<State>({ status: 'loading' })

  useEffect(() => {
    fetch(`/api/spend/surfaces/${encodeURIComponent(surface)}`)
      .then(async (r): Promise<State> => {
        if (r.status === 404) return { status: 'unknown' }
        if (!r.ok) return { status: 'unavailable' }
        const body = await r.json()
        if (!body.github_available || !body.feature) return { status: 'unavailable' }
        return { status: 'ok', issue: body.feature.issue, dollars: body.feature.dollars }
      })
      .catch((): State => ({ status: 'unavailable' }))
      .then(setState)
  }, [surface])

  if (state.status === 'loading') return null
  if (state.status === 'unavailable') return <small>Spend unavailable</small>
  if (state.status === 'unknown') return <small>Unknown UI Surface: {surface}</small>
  return (
    <small>
      <Link to={`/spend/features/${state.issue}`}>Built for {formatDollars(state.dollars)}</Link>
    </small>
  )
}
