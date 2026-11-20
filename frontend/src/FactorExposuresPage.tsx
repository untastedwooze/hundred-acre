import { useState } from 'react'
import type { FormEvent } from 'react'
import SpendBadge from './spend/SpendBadge'
import SeriesChart from './factors/SeriesChart'

type Beta = { factor: string; value: number; t: number }
type Point = { window_end: string; alpha: number; r_squared: number; betas: Beta[] }
type Vintage = { vintage: string; through_date: string }
type Exposures = { series: Point[]; vintage: Vintage | null; empty_reason: string | null; message: string | null }
type State = { status: 'idle' } | { status: 'loading' } | { status: 'error' } | { status: 'ok'; data: Exposures }

const MODELS = ['ff6', 'ff3', 'capm'] as const
type Model = (typeof MODELS)[number]

const ATTRIBUTION = 'Factor Returns: Kenneth R. French Data Library (Tuck School of Business at Dartmouth).'

/** Rolling Factor Exposures of one Security: each coefficient, alpha (daily) and R² over time. */
export default function FactorExposuresPage() {
  const [input, setInput] = useState('')
  const [ticker, setTicker] = useState<string | null>(null)
  const [model, setModel] = useState<Model>('ff6')
  const [state, setState] = useState<State>({ status: 'idle' })

  function load(t: string, m: Model) {
    setState({ status: 'loading' })
    fetch(`/api/market/factors/exposures?ticker=${encodeURIComponent(t)}&model=${m}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('bad response'))))
      .then((data: Exposures) => setState({ status: 'ok', data }))
      .catch(() => setState({ status: 'error' }))
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    const t = input.trim().toUpperCase()
    if (!t) return
    setTicker(t)
    load(t, model)
  }

  function pick(m: Model) {
    setModel(m)
    if (ticker) load(ticker, m)
  }

  const data = state.status === 'ok' ? state.data : null
  const factors = data?.series[0]?.betas.map((b) => b.factor) ?? []

  return (
    <>
      <h1>Factor Exposures</h1>
      <SpendBadge surface="factor-exposures" />
      <form onSubmit={submit}>
        <label>
          Ticker <input value={input} onChange={(e) => setInput(e.target.value)} />
        </label>
        <button type="submit">Show</button>
        <fieldset>
          <legend>Model</legend>
          {MODELS.map((m) => (
            <label key={m}>
              <input type="radio" name="model" checked={model === m} onChange={() => pick(m)} /> {m === 'capm' ? 'CAPM' : m.toUpperCase()}
            </label>
          ))}
        </fieldset>
      </form>
      {state.status === 'loading' && <p>Loading…</p>}
      {state.status === 'error' && <p>Could not load Factor Exposures.</p>}
      {data?.vintage && (
        <p>
          Factor Return vintage {data.vintage.vintage}, through {data.vintage.through_date}
        </p>
      )}
      {data && data.series.length === 0 && <p>{data.message}</p>}
      {data && data.series.length > 0 && (
        <section>
          {factors.map((f, i) => (
            <SeriesChart
              key={f}
              label={f}
              points={data.series.map((p) => ({ date: p.window_end, value: p.betas[i].value }))}
            />
          ))}
          <SeriesChart label="Alpha" unit="daily" points={data.series.map((p) => ({ date: p.window_end, value: p.alpha }))} />
          <SeriesChart label="R²" points={data.series.map((p) => ({ date: p.window_end, value: p.r_squared }))} />
        </section>
      )}
      <p>{ATTRIBUTION}</p>
    </>
  )
}
