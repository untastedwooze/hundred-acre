import { formatDollars, formatLocal } from './format'

export type SeriesPoint = { timestamp: string; dollars: string }

const W = 640
const H = 240
const PAD = { top: 16, right: 16, bottom: 28, left: 56 }

/** Step-after line chart of cumulative Spend. Pure SVG, themed via CSS variables. */
export default function CumulativeSpendChart({ points }: { points: SeriesPoint[] }) {
  const times = points.map((p) => Date.parse(p.timestamp))
  const values = points.map((p) => Number(p.dollars))
  const t0 = Math.min(...times)
  const t1 = Math.max(...times)
  const max = Math.max(...values) || 1
  const x = (t: number) => PAD.left + (t1 === t0 ? 0.5 : (t - t0) / (t1 - t0)) * (W - PAD.left - PAD.right)
  const y = (v: number) => PAD.top + (1 - v / max) * (H - PAD.top - PAD.bottom)
  const baseline = y(0)

  let path = `M ${x(times[0])} ${baseline}`
  values.forEach((v, i) => {
    path += ` L ${x(times[i])} ${i === 0 ? baseline : y(values[i - 1])} L ${x(times[i])} ${y(v)}`
  })

  return (
    <svg
      role="img"
      aria-label="Cumulative Spend over time"
      viewBox={`0 0 ${W} ${H}`}
      style={{ width: '100%', height: 'auto', maxWidth: W }}
    >
      <line x1={PAD.left} x2={W - PAD.right} y1={baseline} y2={baseline} stroke="var(--border)" />
      <line x1={PAD.left} x2={PAD.left} y1={PAD.top} y2={baseline} stroke="var(--border)" />
      <text x={PAD.left - 8} y={y(max)} textAnchor="end" dominantBaseline="middle" fill="var(--text)" fontSize="12">
        {formatDollars(max)}
      </text>
      <text x={PAD.left - 8} y={baseline} textAnchor="end" dominantBaseline="middle" fill="var(--text)" fontSize="12">
        $0.00
      </text>
      <path d={path} fill="none" stroke="var(--accent)" strokeWidth="2" />
      {points.map((p, i) => (
        <circle key={p.timestamp} cx={x(times[i])} cy={y(values[i])} r="3" fill="var(--accent)">
          <title>{`${formatLocal(p.timestamp)}: ${formatDollars(p.dollars)}`}</title>
        </circle>
      ))}
    </svg>
  )
}
