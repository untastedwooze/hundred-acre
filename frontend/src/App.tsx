import { useEffect, useState } from 'react'
import { Route, Routes } from 'react-router-dom'
import Header from './Header'
import SpendPage from './SpendPage'

type Health = 'loading' | 'ok' | 'unreachable'

function Home() {
  const [health, setHealth] = useState<Health>('loading')

  useEffect(() => {
    fetch('/api/health')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('bad response'))))
      .then((body) => setHealth(body.status === 'ok' ? 'ok' : 'unreachable'))
      .catch(() => setHealth('unreachable'))
  }, [])

  return <p>API status: {health}</p>
}

/** Rendered inside a Router (BrowserRouter in main.tsx, MemoryRouter in tests). */
function App() {
  return (
    <>
      <Header />
      <main>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/spend" element={<SpendPage />} />
        </Routes>
      </main>
    </>
  )
}

export default App
