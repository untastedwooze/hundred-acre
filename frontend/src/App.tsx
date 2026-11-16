import { useEffect, useState } from 'react'
import FeatureTable from './FeatureTable'
import Header from './Header'

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
    <>
      <Header />
      <main>
        <p>API status: {health}</p>
        <FeatureTable />
      </main>
    </>
  )
}

export default App
