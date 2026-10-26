// Every backend call goes through apiFetch: the base comes from VITE_API_BASE (default /api;
// in dev the Vite server proxies /api to the backend, in production Caddy does, so the
// browser only ever talks to its own origin and the backend needs no CORS), the session
// cookie is sent with every request, and a 401 from any route but the login itself tells
// the app to show the login page.

// Only a path ("/api") or a URL is accepted: Git Bash on Windows rewrites VITE_API_BASE=/api into
// "C:/Program Files/Git/api" unless MSYS_NO_PATHCONV=1, and that must not ship.
const configured = import.meta.env?.VITE_API_BASE
export const API_BASE = (/^(\/|https?:\/\/)/.test(configured ?? '') ? configured : '/api').replace(/\/+$/, '')

export const apiUrl = (path) => `${API_BASE}${path}`

const listeners = new Set()

// Called on every 401 (session missing or expired). Returns an unsubscribe function.
export function onUnauthorized(fn) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export async function apiFetch(path, options = {}) {
  const res = await fetch(apiUrl(path), { credentials: 'include', ...options })
  if (res.status === 401 && !path.startsWith('/auth/login')) listeners.forEach((fn) => fn())
  return res
}

// Login: {ok: true} or {ok: false, message} with the reason a person can act on.
export async function login(password) {
  let res
  try {
    res = await apiFetch('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    })
  } catch {
    return { ok: false, message: 'Can’t reach the server. Check your connection and try again.' }
  }
  if (res.ok) return { ok: true }
  if (res.status === 401) return { ok: false, message: 'Wrong password.' }
  if (res.status === 429) return { ok: false, message: 'Too many failed attempts. Try again in 15 minutes.' }
  if (res.status === 503) return { ok: false, message: 'Login isn’t set up on the server yet (run python auth.py set-password).' }
  return { ok: false, message: `Login failed (${res.status}).` }
}

export async function logout() {
  try {
    await apiFetch('/auth/logout', { method: 'POST' })
  } catch {
    // the cookie is cleared server-side; the app shows the login page either way
  }
}

// true with a valid session, false without one; null when the server can't be reached
export async function checkSession() {
  try {
    const res = await apiFetch('/auth/me')
    return res.ok
  } catch {
    return null
  }
}
