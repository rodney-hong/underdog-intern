import { useState, useEffect, useRef, useCallback } from 'react'
import History from './History'

const API = 'http://localhost:8000'
const STAT_TYPES = ['Points', 'Assists', 'Rebounds', 'PRA', '3PM', 'PR', 'PA', 'RA']

// ---------------------------------------------------------------------------
// Theme icons
// ---------------------------------------------------------------------------

function SunIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="5"/>
      <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>
    </svg>
  )
}

function MoonIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
    </svg>
  )
}

// ---------------------------------------------------------------------------
// Utility components
// ---------------------------------------------------------------------------

function ClearButton({ onClick, visible }) {
  if (!visible) return null
  return (
    <button
      type="button"
      onClick={onClick}
      className="icon-btn absolute right-3 top-1/2 -translate-y-1/2 text-lg leading-none"
      aria-label="Clear"
    >
      ×
    </button>
  )
}

function FieldWrapper({ children, visible }) {
  return (
    <div className={`transition-all duration-300 ${visible ? 'animate-slide-down opacity-100' : 'opacity-0 pointer-events-none h-0 overflow-hidden'}`}>
      {children}
    </div>
  )
}

function Label({ children }) {
  return (
    <label className="block text-xs font-semibold uppercase tracking-widest mb-1.5" style={{ color: 'var(--text-muted)' }}>
      {children}
    </label>
  )
}

// ---------------------------------------------------------------------------
// Player Search with autocomplete dropdown
// ---------------------------------------------------------------------------

function PlayerSearch({ value, onChange, onSelect, onClear }) {
  const [suggestions, setSuggestions] = useState([])
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)
  const debounceRef = useRef(null)
  const containerRef = useRef(null)

  const fetchSuggestions = useCallback(async (q) => {
    if (!q.trim()) { setSuggestions([]); setOpen(false); return }
    setLoading(true)
    try {
      const res = await fetch(`${API}/players/search?q=${encodeURIComponent(q)}`)
      const data = await res.json()
      setSuggestions(data)
      setOpen(data.length > 0)
    } catch {
      setSuggestions([])
    } finally {
      setLoading(false)
    }
  }, [])

  const handleChange = (e) => {
    const q = e.target.value
    onChange(q)
    clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => fetchSuggestions(q), 300)
  }

  const handleSelect = (name) => {
    setOpen(false)
    setSuggestions([])
    onSelect(name)
  }

  const handleClear = () => {
    setSuggestions([])
    setOpen(false)
    onClear()
  }

  useEffect(() => {
    const handler = (e) => {
      if (containerRef.current && !containerRef.current.contains(e.target)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  return (
    <div ref={containerRef} className="relative">
      <Label>Player Search</Label>
      <div className="relative">
        <input
          type="text"
          value={value}
          onChange={handleChange}
          onFocus={() => suggestions.length > 0 && setOpen(true)}
          placeholder="Search NBA players…"
          className="input-field w-full rounded-xl px-4 py-3 pr-10"
        />
        {loading && (
          <span className="absolute right-3 top-1/2 -translate-y-1/2 text-sm" style={{ color: 'var(--text-muted)' }}>…</span>
        )}
        {!loading && <ClearButton visible={!!value} onClick={handleClear} />}
      </div>
      {open && (
        <ul className="card absolute z-50 mt-1 w-full rounded-xl shadow-2xl overflow-hidden animate-slide-down">
          {suggestions.map((name) => (
            <li
              key={name}
              onMouseDown={() => handleSelect(name)}
              className="suggestion-item px-4 py-2.5 cursor-pointer text-sm"
            >
              {name}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Read-only team field
// ---------------------------------------------------------------------------

function TeamField({ team, onClear }) {
  return (
    <div>
      <Label>Player's Team</Label>
      <div className="relative">
        <div className="input-field w-full rounded-xl px-4 py-3 pr-10 select-none" style={{ color: 'var(--text-muted)' }}>
          {team || <span style={{ opacity: 0.5 }}>Loading…</span>}
        </div>
        <ClearButton visible={!!team} onClick={onClear} />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Opponent team dropdown
// ---------------------------------------------------------------------------

function OpponentField({ value, teams, playerTeam, onChange, onClear }) {
  const filtered = teams.filter((t) => t !== playerTeam)
  return (
    <div>
      <Label>Opponent Team</Label>
      <div className="relative">
        <select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="input-field w-full rounded-xl px-4 py-3 pr-10 appearance-none cursor-pointer"
        >
          <option value="">Select opponent…</option>
          {filtered.map((t) => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
        {!value && (
          <span className="absolute right-3 top-1/2 -translate-y-1/2 pointer-events-none text-xs" style={{ color: 'var(--text-muted)' }}>▾</span>
        )}
        <ClearButton visible={!!value} onClick={onClear} />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Stat line number input
// ---------------------------------------------------------------------------

function StatLineField({ value, onChange, onClear }) {
  const handleChange = (e) => {
    const raw = e.target.value
    if (/^(\d*\.?\d*)$/.test(raw)) onChange(raw)
  }
  return (
    <div>
      <Label>Stat Line</Label>
      <div className="relative">
        <input
          type="text"
          inputMode="decimal"
          value={value}
          onChange={handleChange}
          placeholder="e.g. 24.5"
          className="input-field w-full rounded-xl px-4 py-3 pr-10"
        />
        <ClearButton visible={!!value} onClick={onClear} />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Stat type dropdown
// ---------------------------------------------------------------------------

function StatTypeField({ value, onChange, onClear }) {
  return (
    <div>
      <Label>Stat Type</Label>
      <div className="relative">
        <select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="input-field w-full rounded-xl px-4 py-3 pr-10 appearance-none cursor-pointer"
        >
          <option value="">Select stat type…</option>
          {STAT_TYPES.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
        {!value && (
          <span className="absolute right-3 top-1/2 -translate-y-1/2 pointer-events-none text-xs" style={{ color: 'var(--text-muted)' }}>▾</span>
        )}
        <ClearButton visible={!!value} onClick={onClear} />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Injury / news context components
// ---------------------------------------------------------------------------

const STATUS_CLS = {
  Out:          'status-out',
  Questionable: 'status-questionable',
  Probable:     'status-neutral',
}

function InjuryBanner({ status, reason }) {
  if (!status) return null
  const cls = STATUS_CLS[status] ?? 'status-neutral'
  return (
    <div className={`animate-fade-in flex items-start gap-2 border rounded-xl px-4 py-2.5 text-sm ${cls}`}>
      <span className="font-semibold shrink-0">{status}</span>
      {reason && <span className="opacity-80">— {reason}</span>}
    </div>
  )
}

function NewsNote({ text }) {
  if (!text) return null
  return (
    <div className="animate-fade-in text-xs italic px-1" style={{ color: 'var(--text-muted)' }}>
      📰 {text}
    </div>
  )
}

function OpponentInjuryReport({ injuries, opponentName }) {
  if (!injuries || injuries.length === 0) return null
  return (
    <div className="card animate-fade-in mt-4 rounded-2xl p-5 space-y-3">
      <p className="text-xs font-semibold uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>
        {opponentName} Injury Report
      </p>
      <ul className="space-y-2">
        {injuries.map((inj, i) => {
          const cls = STATUS_CLS[inj.status] ?? 'status-neutral'
          return (
            <li key={i} className={`flex items-start gap-2 border rounded-xl px-3 py-2 text-sm ${cls}`}>
              <span className="font-medium shrink-0">{inj.player}</span>
              <span className="font-semibold shrink-0">({inj.status})</span>
              {inj.reason && <span className="opacity-70">— {inj.reason}</span>}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Result card
// ---------------------------------------------------------------------------

function ResultCard({ result }) {
  const isOver = result.outcome === 'OVER'
  return (
    <div className="card animate-fade-in mt-6 rounded-2xl p-6 space-y-4">
      <div className="flex items-center gap-4">
        <span className="text-4xl font-black tracking-tight" style={{ color: isOver ? 'var(--success)' : 'var(--danger)' }}>
          {result.outcome}
        </span>
        <div className="flex flex-col">
          <span className="text-xs uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>Confidence</span>
          <span className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>{result.confidence}%</span>
        </div>
        <div className="flex-1 h-2 rounded-full overflow-hidden" style={{ backgroundColor: 'var(--bg-tertiary)' }}>
          <div
            className="h-full rounded-full transition-all duration-700"
            style={{ width: `${result.confidence}%`, backgroundColor: isOver ? 'var(--success)' : 'var(--danger)' }}
          />
        </div>
      </div>
      <p className="text-sm leading-relaxed border-t pt-4" style={{ color: 'var(--text-secondary)', borderTopColor: 'var(--border)' }}>
        {result.explanation}
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main App
// ---------------------------------------------------------------------------

export default function App() {
  // Initialise theme synchronously to avoid flash of wrong theme
  const [theme, setTheme] = useState(() => {
    const stored = localStorage.getItem('theme')
    const preferred = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
    const t = stored || preferred
    document.documentElement.setAttribute('data-theme', t)
    return t
  })

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem('theme', theme)
  }, [theme])

  const toggleTheme = () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))

  const [currentPage, setCurrentPage] = useState('predict')
  const [playerQuery, setPlayerQuery] = useState('')
  const [selectedPlayer, setSelectedPlayer] = useState('')
  const [playerTeam, setPlayerTeam] = useState('')
  const [allTeams, setAllTeams] = useState([])
  const [opponent, setOpponent] = useState('')
  const [statLine, setStatLine] = useState('')
  const [statType, setStatType] = useState('')
  const [isHome, setIsHome] = useState(false)
  const [isBackToBack, setIsBackToBack] = useState(false)
  const [playerContext, setPlayerContext] = useState(null)
  const [opponentInjuries, setOpponentInjuries] = useState([])
  const [result, setResult] = useState(null)
  const [predicting, setPredicting] = useState(false)
  const [error, setError] = useState('')

  const showAdditionalFields = !!selectedPlayer

  useEffect(() => {
    fetch(`${API}/teams`)
      .then((r) => r.json())
      .then(setAllTeams)
      .catch(() => {})
  }, [])

  useEffect(() => {
    if (!selectedPlayer) return
    setPlayerTeam('')
    setOpponent('')
    setIsHome(false)
    setIsBackToBack(false)
    setPlayerContext(null)
    fetch(`${API}/players/${encodeURIComponent(selectedPlayer)}/team`)
      .then((r) => r.json())
      .then((data) => {
        setPlayerTeam(data.player_team?.full || '')
        if (data.next_opponent?.full) setOpponent(data.next_opponent.full)
        setIsHome(data.is_home ?? false)
        setIsBackToBack(data.is_back_to_back ?? false)
      })
      .catch(() => { setPlayerTeam('') })
    fetch(`${API}/players/${encodeURIComponent(selectedPlayer)}/context`)
      .then((r) => r.json())
      .then(setPlayerContext)
      .catch(() => {})
  }, [selectedPlayer])

  const handlePlayerSelect = (name) => {
    setSelectedPlayer(name)
    setPlayerQuery(name)
    setResult(null)
    setError('')
  }

  const resetAll = () => {
    setPlayerQuery('')
    setSelectedPlayer('')
    setPlayerTeam('')
    setOpponent('')
    setStatLine('')
    setStatType('')
    setIsHome(false)
    setIsBackToBack(false)
    setPlayerContext(null)
    setOpponentInjuries([])
    setResult(null)
    setError('')
  }

  const canPredict =
    selectedPlayer && playerTeam && opponent && statLine && parseFloat(statLine) > 0 && statType

  const handlePredict = async () => {
    if (!canPredict) return
    setPredicting(true)
    setError('')
    setResult(null)
    try {
      const res = await fetch(`${API}/predict`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          player_name: selectedPlayer,
          opponent_team: opponent,
          stat_line: parseFloat(statLine),
          stat_type: statType,
          is_home: isHome,
          is_back_to_back: isBackToBack,
        }),
      })
      if (!res.ok) {
        const err = await res.json()
        throw new Error(err.detail || 'Prediction failed')
      }
      const data = await res.json()
      setResult(data)
      setOpponentInjuries(data.opponent_injuries ?? [])
    } catch (e) {
      setError(e.message || 'Something went wrong. Is the backend running?')
    } finally {
      setPredicting(false)
    }
  }

  return (
    <div className="min-h-screen flex flex-col items-center px-4 py-16" style={{ backgroundColor: 'var(--bg-primary)' }}>
      {/* Header */}
      <div className="text-center mb-8">
        <div className="text-5xl mb-3">🏀</div>
        <h1 className="text-3xl font-black tracking-tight" style={{ color: 'var(--text-primary)' }}>NBA Prop Predictor</h1>
        <p className="mt-1 text-sm" style={{ color: 'var(--text-muted)' }}>AI-powered over/under predictions using real game data</p>
      </div>

      {/* Nav bar */}
      <div className="card flex items-center gap-1 rounded-xl p-1 mb-8">
        {['predict', 'history'].map((page) => (
          <button
            key={page}
            onClick={() => setCurrentPage(page)}
            className={`px-5 py-2 rounded-lg text-sm font-semibold capitalize transition-all duration-200 ${
              currentPage === page ? 'nav-tab-active' : 'nav-tab'
            }`}
          >
            {page === 'predict' ? 'Predict' : 'History'}
          </button>
        ))}
        <button
          onClick={toggleTheme}
          className="theme-toggle ml-2 p-2 rounded-lg"
          aria-label={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
          title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
        >
          {theme === 'dark' ? <MoonIcon /> : <SunIcon />}
        </button>
      </div>

      {currentPage === 'history' && <History />}

      {currentPage === 'predict' && <>
        {/* Form card */}
        <div className="card w-full max-w-lg rounded-2xl p-6 shadow-2xl space-y-4">
          <PlayerSearch
            value={playerQuery}
            onChange={setPlayerQuery}
            onSelect={handlePlayerSelect}
            onClear={resetAll}
          />

          {playerContext?.injury_status && (
            <InjuryBanner
              status={playerContext.injury_status.status}
              reason={playerContext.injury_status.reason}
            />
          )}
          {playerContext?.news && <NewsNote text={playerContext.news} />}

          <FieldWrapper visible={showAdditionalFields}>
            <div className="space-y-4 pt-1">
              <TeamField team={playerTeam} onClear={resetAll} />
              <OpponentField
                value={opponent}
                teams={allTeams}
                playerTeam={playerTeam}
                onChange={setOpponent}
                onClear={() => { setOpponent(''); setResult(null) }}
              />
              <StatLineField
                value={statLine}
                onChange={setStatLine}
                onClear={() => { setStatLine(''); setResult(null) }}
              />
              <StatTypeField
                value={statType}
                onChange={setStatType}
                onClear={() => { setStatType(''); setResult(null) }}
              />
            </div>
          </FieldWrapper>

          <FieldWrapper visible={showAdditionalFields}>
            <button
              onClick={handlePredict}
              disabled={!canPredict || predicting}
              className="btn-accent w-full mt-2 py-3.5 rounded-xl font-bold text-sm uppercase tracking-widest"
            >
              {predicting ? (
                <span className="flex items-center justify-center gap-2">
                  <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z" />
                  </svg>
                  Analyzing…
                </span>
              ) : (
                'Predict'
              )}
            </button>
          </FieldWrapper>

          {error && (
            <div className="animate-fade-in status-out border rounded-xl px-4 py-3 text-sm">
              {error}
            </div>
          )}
        </div>

        {result && (
          <div className="w-full max-w-lg">
            <ResultCard result={result} />
            <OpponentInjuryReport injuries={opponentInjuries} opponentName={opponent} />
            <button
              onClick={resetAll}
              className="btn-ghost mt-4 w-full py-3 rounded-xl text-sm font-medium"
            >
              Clear All
            </button>
          </div>
        )}
      </>}
    </div>
  )
}
