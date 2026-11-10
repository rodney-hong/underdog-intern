import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch } from './api'

// Read-only "Ask" assistant (POST /assistant), shown inside History: a side panel on wide screens,
// a bottom sheet otherwise. The server's LLM only phrases numbers that its whitelisted read-only
// tools return; nothing here changes a prediction.
const EXAMPLES = [
  'From the pending predictions, which have the best confidence and track record?',
  'Explain the confidence on Ja Morant threes',
  'Any injury news for Memphis?',
]

const TOOL_LABELS = { list_pending: 'pending picks', track_record: 'track record', explain: 'explain',
                      injury_context: 'injury news' }

// Conversation state, kept by History so the panel can close and reopen without losing it.
// explainPick(p) asks about that exact pick: its id goes to the server, which explains that row.
export function useAsk() {
  const [messages, setMessagesState] = useState([])    // { role, content, tools? }
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const busyRef = useRef(false)
  const messagesRef = useRef([])
  const setMessages = (next) => { messagesRef.current = next; setMessagesState(next) }

  const send = useCallback(async (text, predictionId) => {
    const question = text.trim()
    if (!question || busyRef.current) return
    busyRef.current = true
    const history = [...messagesRef.current, { role: 'user', content: question }]
    setMessages(history)
    setError(null)
    setBusy(true)
    try {
      const body = { messages: history.slice(-20).map(({ role, content }) => ({ role, content })) }
      if (predictionId != null) body.prediction_id = predictionId
      const res = await apiFetch('/assistant', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${res.status})`)
      setMessages([...history, { role: 'assistant', content: data.answer || '(no answer)', tools: data.tools_used || [] }])
    } catch (e) {
      setError(e.message === 'Failed to fetch' ? 'Can’t reach the server.' : e.message)
    } finally {
      busyRef.current = false
      setBusy(false)
    }
  }, [])   // setMessages only touches a ref and a state setter

  const explainPick = useCallback((p) => send(
    `Explain the confidence on ${p.player_name} ${p.stat_type} ${p.stat_line} ${p.predicted_outcome}` +
    ` (game ${p.game_date ?? p.timestamp?.slice(0, 10)})`, p.id), [send])

  return { messages, busy, error, send, explainPick }
}

const CLOSE = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
       strokeLinecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
)

// variant: 'side' (sticky column beside the table) or 'sheet' (fixed to the bottom of the screen)
export function AskPanel({ ask, onClose, variant = 'side' }) {
  const [draft, setDraft] = useState('')
  const endRef = useRef(null)
  const { messages, busy, error, send } = ask

  useEffect(() => { endRef.current?.scrollIntoView?.({ block: 'end' }) }, [messages, busy])

  const submit = () => { if (draft.trim() && !busy) { send(draft); setDraft('') } }
  const onKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit() }
  }

  return (
    <aside aria-label="Ask" className={`card ask-panel ask-${variant} flex flex-col`}>
      <div className="flex items-center justify-between gap-2 px-4 py-3" style={{ borderBottom: '1px solid var(--border)' }}>
        <div className="min-w-0">
          <h2 className="panel-title">Ask</h2>
          <p className="text-xs" style={{ color: 'var(--text-muted)' }}>Read-only. No model has a measured edge.</p>
        </div>
        <button onClick={onClose} className="icon-btn tap flex items-center justify-center" aria-label="Close Ask">
          {CLOSE}
        </button>
      </div>

      <div className="ask-scroll flex-1 min-h-0 overflow-y-auto px-4 py-3 space-y-3">
        {messages.length === 0 && (
          <div className="space-y-2">
            <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
              Try one of these, or tap the explain button on a pick.
            </p>
            {EXAMPLES.map((q) => (
              <button key={q} onClick={() => send(q)} className="btn-ghost tap w-full text-left px-3 py-2 text-sm">
                {q}
              </button>
            ))}
          </div>
        )}
        <ul className="space-y-3" aria-live="polite">
          {messages.map((m, i) => (
            <li key={i} className={m.role === 'user' ? 'flex justify-end' : ''}>
              <div className={`rounded-md px-3 py-2 text-sm whitespace-pre-wrap break-words ${m.role === 'user' ? 'max-w-[85%]' : ''}`}
                   style={{ backgroundColor: m.role === 'user' ? 'var(--raised)' : 'transparent' }}>
                {m.content}
                {m.tools?.length > 0 && (
                  <div className="mt-2 text-xs" style={{ color: 'var(--text-muted)' }}>
                    From: {[...new Set(m.tools.map((t) => TOOL_LABELS[t.name] || t.name))].join(', ')}
                  </div>
                )}
              </div>
            </li>
          ))}
          {busy && <li className="text-sm" style={{ color: 'var(--text-muted)' }}>Looking it up…</li>}
        </ul>
        {error && <div role="alert" className="notice px-3 py-2 text-sm">{error}</div>}
        <div ref={endRef} />
      </div>

      <form onSubmit={(e) => { e.preventDefault(); submit() }} className="ask-form flex gap-2 items-end px-4 py-3"
            style={{ borderTop: '1px solid var(--border)' }}>
        <label htmlFor="ask-input" className="sr-only">Question</label>
        <textarea id="ask-input" rows={2} value={draft} onChange={(e) => setDraft(e.target.value)} onKeyDown={onKeyDown}
                  maxLength={4000} placeholder="Ask about your picks…"
                  className="input-field flex-1 min-w-0 px-3 py-2 text-base sm:text-sm resize-none" />
        <button type="submit" disabled={busy || !draft.trim()} className="btn-accent tap px-4 py-2.5 text-sm">
          Send
        </button>
      </form>
    </aside>
  )
}
