export type SeriesPoint = { date: string; value: number }

const W = 320
const H = 140
const PAD = { top: 12, right: 12, bottom: 22, left: 56 }

const fmt = (v: number) => (Math.abs(v) < 0.01 && v !== 0 ? v.toExponential(1) : v.toFixed(2))

/** Small line chart of one series over time. Pure SVG, themed via CSS variables. */
export default function SeriesChart({ label, unit, points }: { label: string; unit?: string; points: SeriesPoint[] }) {
  const times = points.map((p) => Date.parse(p.date))
  const values = points.map((p) => p.value)
  const t0 = Math.min(...times)
  const t1 = Math.max(...times)
  const lo = Math.min(...values, 0)
  const hi = Math.max(...values, 0)
  const span = hi - lo || 1
  const x = (t: number) => PAD.left + (t1 === t0 ? 0.5 : (t - t0) / (t1 - t0)) * (W - PAD.left - PAD.right)
  const y = (v: number) => PAD.top + (1 - (v - lo) / span) * (H - PAD.top - PAD.bottom)
  const path = points.map((p, i) => `${i ? 'L' : 'M'} ${x(times[i])} ${y(p.value)}`).join(' ')
  const title = `${label}${unit ? ` (${unit})` : ''}`

  return (
    <figure style={{ margin: 0, display: 'inline-block' }}>
      <svg role="img" aria-label={`${label} over time`} viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', maxWidth: W, height: 'auto' }}>
        <line x1={PAD.left} x2={W - PAD.right} y1={y(0)} y2={y(0)} stroke="var(--border)" />
        <text x={PAD.left - 6} y={y(hi)} textAnchor="end" dominantBaseline="middle" fill="var(--text)" fontSize="11">{fmt(hi)}</text>
        <text x={PAD.left - 6} y={y(lo)} textAnchor="end" dominantBaseline="middle" fill="var(--text)" fontSize="11">{fmt(lo)}</text>
        <text x={PAD.left} y={H - 6} fill="var(--text)" fontSize="11">{points[0].date}</text>
        <text x={W - PAD.right} y={H - 6} textAnchor="end" fill="var(--text)" fontSize="11">{points[points.length - 1].date}</text>
        <path d={path} fill="none" stroke="var(--accent)" strokeWidth="2" />
        {points.map((p, i) => (
          <circle key={p.date} cx={x(times[i])} cy={y(p.value)} r="2.5" fill="var(--accent)">
            <title>{`${p.date}: ${fmt(p.value)}`}</title>
          </circle>
        ))}
      </svg>
      <figcaption>{title}</figcaption>
    </figure>
  )
}
