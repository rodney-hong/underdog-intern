// Run: npx vitest run (from frontend/). Every request goes to /api/... with the session cookie;
// the app shows the login page without a session, opens after logging in, and returns to
// the login page on logout or on any 401 (session expired).
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import App from './App'
import { apiFetch, login } from './api'

let session
const json = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body })

function server(url, opts = {}) {
  const path = url.replace(/^\/api/, '')
  if (path === '/auth/me') return json(session ? 200 : 401, session ? { ok: true } : { detail: 'Not logged in' })
  if (path === '/auth/login') {
    const { password } = JSON.parse(opts.body)
    if (password === 'right one') { session = true; return json(200, { ok: true }) }
    return json(password === 'locked' ? 429 : 401, { detail: 'x' })
  }
  if (path === '/auth/logout') { session = false; return json(200, { ok: true }) }
  if (!session) return json(401, { detail: 'Not logged in' })
  if (path === '/teams') return json(200, ['Boston Celtics', 'Utah Jazz'])
  if (path === '/predictions/history') return json(200, [])
  return json(200, {})
}

beforeEach(() => {
  session = false
  localStorage.clear()
  globalThis.fetch = vi.fn(async (url, opts) => server(url, opts))
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('every request goes to /api with the session cookie', async () => {
  await apiFetch('/teams')
  expect(globalThis.fetch).toHaveBeenCalledWith('/api/teams', expect.objectContaining({ credentials: 'include' }))
})

it('login explains a wrong password and a lockout', async () => {
  expect(await login('nope')).toEqual({ ok: false, message: 'Wrong password.' })
  expect((await login('locked')).message).toMatch(/Too many failed attempts/)
  expect(await login('right one')).toEqual({ ok: true })
})

it('shows the login page without a session, then the app; logout returns to it', async () => {
  render(<App />)
  const pw = await screen.findByLabelText('Password')
  expect(screen.queryByText('Predict')).toBeNull()                 // no nav before logging in
  fireEvent.change(pw, { target: { value: 'wrong' } })
  fireEvent.click(screen.getByRole('button', { name: 'Log in' }))
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Wrong password.')

  fireEvent.change(pw, { target: { value: 'right one' } })
  fireEvent.click(screen.getByRole('button', { name: 'Log in' }))
  expect(await screen.findByRole('button', { name: 'History' })).toBeTruthy()
  expect(screen.queryByLabelText('Password')).toBeNull()

  fireEvent.click(screen.getByRole('button', { name: 'Log out' }))
  expect(await screen.findByLabelText('Password')).toBeTruthy()
})

it('any 401 after logging in sends the app back to the login page', async () => {
  session = true
  render(<App />)
  fireEvent.click(await screen.findByRole('button', { name: 'History' }))
  session = false                                                  // the session expires
  await apiFetch('/predictions/history')
  expect(await screen.findByText(/Your session ended/)).toBeTruthy()
})

it('says so when the server cannot be reached', async () => {
  globalThis.fetch = vi.fn(async () => { throw new TypeError('Failed to fetch') })
  render(<App />)
  expect(await screen.findByText('Can’t reach the server.')).toBeTruthy()
  await waitFor(() => expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy())
})
