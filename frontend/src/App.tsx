import { useEffect, useState } from 'react'

type Health = 'loading' | 'ok' | 'unreachable'

function App() {
  const [health, setHealth] = useState<Health>('loading')

  useEffect(() => {
    fetch('/api/health')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('bad response'))))
      .then((body) => setHealth(body.status === 'ok' ? 'ok' : 'unreachable'))
      .catch(() => setHealth('unreachable'))
  }, [])

  return (
    <main>
      <h1>hundred-acre</h1>
      <p>API status: {health}</p>
    </main>
  )
}

export default App
