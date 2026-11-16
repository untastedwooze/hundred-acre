export function formatDollars(dollars: string | number): string {
  return `$${Number(dollars).toFixed(2)}`
}

/** Timestamps are UTC ISO strings from the API; Intl renders them in the viewer's local zone. */
export function formatLocal(isoUtc: string): string {
  return new Date(isoUtc).toLocaleString('en-US', { dateStyle: 'medium', timeStyle: 'short' })
}
