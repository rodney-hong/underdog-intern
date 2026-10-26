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
    <form onSubmit={submit} className="card animate-fade-in w-full max-w-sm rounded-2xl p-6 shadow-2xl space-y-4">
      <div>
        <h2 className="text-lg font-bold" style={{ color: 'var(--text-primary)' }}>Log in</h2>
        <p className="mt-1 text-sm" style={{ color: 'var(--text-muted)' }}>
          {expired ? 'Your session ended. Log in again to continue.' : 'Enter the admin password.'}
        </p>
      </div>
      <div>
        <label htmlFor="password" className="block text-xs font-semibold uppercase tracking-widest mb-1.5"
               style={{ color: 'var(--text-muted)' }}>
          Password
        </label>
        <input
          id="password"
          type="password"
          autoComplete="current-password"
          autoFocus
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="input-field w-full rounded-xl px-4 py-3"
        />
      </div>
      {error && <div role="alert" className="animate-fade-in status-out border rounded-xl px-4 py-3 text-sm">{error}</div>}
      <button type="submit" disabled={!password || busy}
              className="btn-accent w-full py-3.5 rounded-xl font-bold text-sm uppercase tracking-widest">
        {busy ? 'Logging in…' : 'Log in'}
      </button>
    </form>
  )
}
