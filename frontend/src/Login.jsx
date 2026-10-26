import { useState } from 'react'
import { login } from './api'

// The one admin login (backend auth.py). On success the server sets a 30-day session cookie
// and the app opens where it was.
export default function Login({ onLoggedIn, expired = false }) {
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const submit = async (e) => {
    e.preventDefault()
    if (!password || busy) return
    setBusy(true)
    setError('')
    const r = await login(password)
    setBusy(false)
    if (r.ok) {
      setPassword('')
      onLoggedIn()
    } else {
      setError(r.message)
    }
  }

  return (
    <form onSubmit={submit} className="card w-full max-w-sm p-6 space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Log in</h1>
        <p className="mt-1 text-sm" style={{ color: 'var(--text-muted)' }}>
          {expired ? 'Your session ended. Log in again to continue.' : 'Enter the admin password.'}
        </p>
      </div>
      <div>
        <label htmlFor="password" className="field-label">
          Password
        </label>
        <input
          id="password"
          type="password"
          autoComplete="current-password"
          autoFocus
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="input-field w-full px-3 py-2"
        />
      </div>
      {error && <div role="alert" className="notice animate-fade-in px-3 py-2 text-sm">{error}</div>}
      <button type="submit" disabled={!password || busy}
              className="btn-accent tap w-full py-2.5 text-sm">
        {busy ? 'Logging in…' : 'Log in'}
      </button>
    </form>
  )
}
