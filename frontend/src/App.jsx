import { useState, useEffect } from 'react'
import History from './History'
import Login from './Login'
import ScreenshotImport from './ScreenshotImport'
import { apiFetch, checkSession, logout, onUnauthorized } from './api'
import { fetchTrackRecord, pct, rangeStr } from './trackRecord'
import { ClearButton, Label } from './components/fields'
import PlayerSearch from './components/PlayerSearch'

const STAT_TYPES = [
  'Points', 'Assists', 'Rebounds', 'PRA', '3PM', 'PR', 'PA', 'RA',
  'Blocks', 'Steals', 'Blocks+Steals', 'Turnovers',
  'Offensive Rebounds', 'Defensive Rebounds', 'Double Double', '3PA',
]

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

// Hidden fields render nothing (a collapsed wrapper still took its share of the form's
// spacing); they slide in when a player is chosen.
function FieldWrapper({ children, visible }) {
  if (!visible) return null
  return (
    <div className="animate-slide-down">
      {children}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Read-only team field
// ---------------------------------------------------------------------------

function TeamField({ team, onClear }) {
  return (
    <div>
      <Label>Team</Label>
      <div className="relative">
        <div className="input-field w-full px-3 py-2 pr-10 select-none" style={{ backgroundColor: 'var(--raised)', borderColor: 'var(--border)' }}>
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
      <Label>Opponent</Label>
      <div className="relative">
        <select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="input-field w-full px-3 py-2 pr-10 appearance-none cursor-pointer"
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

const SOURCE_LABELS = {
  fanduel:    'FanDuel',
  draftkings: 'DraftKings',
  betmgm:     'BetMGM',
  betonlineag:'BetOnline',
  bovada:     'Bovada',
}

function StatLineField({ value, onChange, onClear, source, lineLoading }) {
  const handleChange = (e) => {
    const raw = e.target.value
    if (/^(\d*\.?\d*)$/.test(raw)) onChange(raw)
  }
  return (
    <div>
      <div className="flex items-baseline justify-between">
        <label className="field-label">Line</label>
        {lineLoading && (
          <span className="text-xs" style={{ color: 'var(--text-muted)' }}>fetching line…</span>
        )}
        {!lineLoading && source && (
          <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
            via {SOURCE_LABELS[source] ?? source}
          </span>
        )}
      </div>
      <div className="relative">
        <input
          type="text"
          inputMode="decimal"
          value={value}
          onChange={handleChange}
          placeholder="e.g. 24.5"
          className="input-field num w-full px-3 py-2 pr-10"
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
      <Label>Stat</Label>
      <div className="relative">
        <select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="input-field w-full px-3 py-2 pr-10 appearance-none cursor-pointer"
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
// Game date input
// ---------------------------------------------------------------------------

function GameDateField({ value, onChange }) {
  return (
    <div>
      <Label>Game date</Label>
      <input
        type="date"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="input-field num w-full px-3 py-2"
      />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Injury / news context components
// ---------------------------------------------------------------------------

// Injury status in words, no colour: the status leads in semibold, the reason follows in grey.
function InjuryBanner({ status, reason }) {
  if (!status || status === 'ambiguous') return null   // unconfirmed name: show nothing
  return (
    <div className="notice animate-fade-in flex items-start gap-2 px-3 py-2 text-sm">
      <span className="font-semibold shrink-0">{status}</span>
      {reason && <span style={{ color: 'var(--text-muted)' }}>{reason}</span>}
    </div>
  )
}

function NewsNote({ text }) {
  if (!text) return null
  return (
    <p className="animate-fade-in text-xs" style={{ color: 'var(--text-muted)' }}>
      News: {text}
    </p>
  )
}

function OpponentInjuryReport({ injuries, opponentName }) {
  if (!injuries || injuries.length === 0) return null
  return (
    <section className="card animate-fade-in p-5">
      <h2 className="panel-title">{opponentName} injury report</h2>
      <ul className="mt-3">
        {injuries.map((inj, i) => (
          <li key={i} className="flex items-baseline gap-3 py-2 text-sm"
              style={i ? { borderTop: '1px solid var(--rule)' } : undefined}>
            <span className="min-w-0 flex-1">{inj.player}</span>
            {inj.reason && <span className="hidden sm:inline" style={{ color: 'var(--text-muted)' }}>{inj.reason}</span>}
            <span className="font-semibold shrink-0">{inj.status}</span>
          </li>
        ))}
      </ul>
    </section>
  )
}

// ---------------------------------------------------------------------------
// Result card
// ---------------------------------------------------------------------------

// Muted, display-only line under the reasoning: the app's record on this player and
// stat, counting only picks resolved before this prediction. Never alters the result.
const TRACK_RECORD_SHOW_N = 5   // below this, only "not enough picks yet"; "small sample" until tr.enough (n >= 20)

function TrackRecordLine({ player, stat, league, predictionId, model }) {
  const [tr, setTr] = useState(null)
  useEffect(() => {
    setTr(null)
    if (!player || !stat || predictionId == null) return
    let live = true
    // the record of the model that made this pick
    fetchTrackRecord({ player, stat, league, before: predictionId, model: model || 'all' })
      .then((d) => { if (live) setTr(d) })
      .catch(() => {})
    return () => { live = false }
  }, [player, stat, league, predictionId, model])
  if (!tr) return null
  return (
    <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
      {tr.n >= TRACK_RECORD_SHOW_N
        ? `Track record: ${tr.wins}–${tr.losses} on ${tr.n} picks · raw ${pct(tr.raw_rate)} (${rangeStr(tr)}) · adjusted ${pct(tr.adjusted_rate)}` +
          (tr.enough ? '' : ' · small sample')
        : `Track record: not enough picks yet (${tr.n})`}
    </p>
  )
}

// The pick and its probability as plain figures in ink: no colour, no fill bar.
function ResultCard({ result, player, stat, league }) {
  return (
    <section className="card animate-fade-in p-5 space-y-4" aria-label="Prediction">
      <div>
        <h2 className="panel-title">{player}</h2>
        <p className="text-sm" style={{ color: 'var(--text-muted)' }}>{stat}</p>
      </div>
      <dl className="grid grid-cols-2" style={{ borderTop: '1px solid var(--rule)', borderBottom: '1px solid var(--rule)' }}>
        <div className="py-3 pr-4">
          <dt className="text-xs" style={{ color: 'var(--text-muted)' }}>Pick</dt>
          <dd className="text-2xl font-semibold">{result.outcome}</dd>
        </div>
        <div className="py-3 pl-4" style={{ borderLeft: '1px solid var(--rule)' }}>
          <dt className="text-xs" style={{ color: 'var(--text-muted)' }}>Confidence</dt>
          <dd className="text-2xl num">{result.confidence}%</dd>
        </div>
      </dl>
      <p className="text-sm leading-relaxed">
        {result.explanation}
      </p>
      <TrackRecordLine player={player} stat={stat} league={league} predictionId={result.prediction_id}
                       model={result.model} />
    </section>
  )
}

// ---------------------------------------------------------------------------
// Slip card
// ---------------------------------------------------------------------------

function SlipCard({ slip, onClear }) {
  if (!slip) return null

  if (slip.picks.length === 0) {
    return (
      <p className="animate-fade-in mt-4 text-sm" style={{ color: 'var(--text-muted)' }}>
        No strong picks found for tonight.
      </p>
    )
  }

  return (
    <div className="animate-fade-in mt-4">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold">Tonight's suggested slip</h3>
        <button onClick={onClear} className="btn-ghost tap text-xs px-3 py-1.5">
          Clear
        </button>
      </div>

      <ul className="mt-2">
        {slip.picks.map((pick, i) => (
          <li key={i} className="py-3 space-y-0.5" style={i ? { borderTop: '1px solid var(--rule)' } : undefined}>
            <div className="flex items-baseline justify-between gap-2">
              <span className="font-medium text-sm">{pick.player}</span>
              <span className="font-semibold text-sm shrink-0">{pick.outcome}</span>
            </div>
            <div className="flex items-baseline justify-between gap-2 text-xs" style={{ color: 'var(--text-muted)' }}>
              <span className="num">{pick.stat_type} · {pick.line}</span>
              <span className="num shrink-0">{Math.round(pick.confidence * 100)}% confidence</span>
            </div>
            <p className="text-xs" style={{ color: 'var(--text-muted)' }}>{pick.game}</p>
          </li>
        ))}
      </ul>

      <div className="flex items-baseline justify-between gap-2 pt-3 text-sm" style={{ borderTop: '1px solid var(--border)' }}>
        <span style={{ color: 'var(--text-muted)' }}>Parlay confidence</span>
        <span className="num font-semibold">{Math.round(slip.parlay_confidence * 100)}%</span>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main App
// ---------------------------------------------------------------------------

function LogoutIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
      <path d="M16 17l5-5-5-5M21 12H9"/>
    </svg>
  )
}

function Spinner() {
  return (
    <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z" />
    </svg>
  )
}

// One choice of a few (NBA / WNBA, slip legs): the chosen label is underlined by a 1px line.
// A 44 px tap target on phones, compact on desktop.
function Pill({ active, onClick, children }) {
  return (
    <button
      onClick={onClick}
      className="choice tap px-2 py-1 text-sm font-medium"
      aria-pressed={active}
    >
      <span className="choice-label">{children}</span>
    </button>
  )
}

// The product's mark: a basketball's seams, drawn in ink.
function Mark({ size = 20 }) {
  return (
    <svg className="mark shrink-0" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="1.6" aria-hidden="true">
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18M12 3v18M6.2 5.6c2.6 1.8 2.6 11 0 12.8M17.8 5.6c-2.6 1.8-2.6 11 0 12.8" />
    </svg>
  )
}

export default function App() {
  // Initialise theme synchronously to avoid flash of wrong theme
  const [theme, setTheme] = useState(() => {
    const stored = localStorage.getItem('theme')
    const preferred = window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
    const t = stored || preferred
    document.documentElement.setAttribute('data-theme', t)
    return t
  })

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem('theme', theme)
  }, [theme])

  const toggleTheme = () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))

  // Session: 'checking' -> 'in' | 'out' | 'expired' (a 401 after being in) | 'offline'
  const [auth, setAuth] = useState('checking')
  const recheck = () => {
    setAuth('checking')
    checkSession().then((ok) => setAuth(ok === null ? 'offline' : ok ? 'in' : 'out'))
  }
  useEffect(recheck, [])
  useEffect(() => onUnauthorized(() => setAuth((a) => (a === 'in' ? 'expired' : a === 'checking' ? 'out' : a))), [])
  const loggedIn = auth === 'in'

  const handleLogout = async () => {
    await logout()
    setAuth('out')
  }

  const [sport, setSport] = useState('NBA')
  const sportPrefix = sport === 'WNBA' ? '/wnba' : ''

  const [currentPage, setCurrentPage] = useState('predict')
  const [playerQuery, setPlayerQuery] = useState('')
  const [selectedPlayer, setSelectedPlayer] = useState('')
  const [playerTeam, setPlayerTeam] = useState('')
  const [allTeams, setAllTeams] = useState([])
  const [wnbaOff, setWnbaOff] = useState(false)
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
  const [lineSource, setLineSource] = useState(null)
  const [lineLoading, setLineLoading] = useState(false)
  const [gameDate, setGameDate] = useState('')
  const [slipResult, setSlipResult] = useState(null)
  const [slipLoading, setSlipLoading] = useState(false)
  const [slipSize, setSlipSize] = useState(2)

  const showAdditionalFields = !!selectedPlayer

  useEffect(() => {
    if (!loggedIn) return
    const prefix = sport === 'WNBA' ? '/wnba' : ''
    setWnbaOff(false)
    apiFetch(`${prefix}/teams`)
      .then((r) => {
        if (sport === 'WNBA' && r.status === 404) {   // WNBA_ENABLED=0 on the server
          setWnbaOff(true)
          return []
        }
        return r.ok ? r.json() : []
      })
      .then(setAllTeams)
      .catch(() => {})
  }, [sport, loggedIn])

  useEffect(() => {
    if (!selectedPlayer) return
    setPlayerTeam('')
    setOpponent('')
    setIsHome(false)
    setIsBackToBack(false)
    setPlayerContext(null)
    setGameDate('')
    const prefix = sport === 'WNBA' ? '/wnba' : ''
    apiFetch(`${prefix}/players/${encodeURIComponent(selectedPlayer)}/team`)
      .then((r) => r.json())
      .then((data) => {
        setPlayerTeam(data.player_team?.full || '')
        if (data.next_opponent?.full) setOpponent(data.next_opponent.full)
        setIsHome(data.is_home ?? false)
        setIsBackToBack(data.is_back_to_back ?? false)
        if (data.next_game_date) setGameDate(data.next_game_date)
      })
      .catch(() => { setPlayerTeam('') })
    apiFetch(`${prefix}/players/${encodeURIComponent(selectedPlayer)}/context`)
      .then((r) => r.json())
      .then(setPlayerContext)
      .catch(() => {})
  }, [selectedPlayer, sport])

  // Auto-fill stat line from OddsAPI when player + stat type are both set
  useEffect(() => {
    if (!selectedPlayer || !statType) {
      setLineSource(null)
      return
    }
    let active = true
    setLineLoading(true)
    setLineSource(null)
    const prefix = sport === 'WNBA' ? '/wnba' : ''
    apiFetch(`${prefix}/lines/${encodeURIComponent(selectedPlayer)}/${encodeURIComponent(statType)}`)
      .then((r) => r.json())
      .then((data) => {
        if (!active) return
        // No line (none logged, or the name isn't confirmed): leave the box empty.
        if (data.line != null) {
          setStatLine(String(data.line))
          setLineSource(data.source ?? null)
        }
      })
      .catch(() => {})
      .finally(() => { if (active) setLineLoading(false) })
    return () => { active = false }
  }, [selectedPlayer, statType, sport])

  const handleBuildSlip = async () => {
    setSlipLoading(true)
    setSlipResult(null)
    try {
      const res = await apiFetch(`/slip?size=${slipSize}`)
      if (!res.ok) throw new Error('slip fetch failed')
      setSlipResult(await res.json())
    } catch {
      setSlipResult({ picks: [], parlay_confidence: 0.0 })
    } finally {
      setSlipLoading(false)
    }
  }

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
    setLineSource(null)
    setLineLoading(false)
    setGameDate('')
  }

  const handleSportChange = (newSport) => {
    if (newSport === sport) return
    setSport(newSport)
    resetAll()
  }

  const canPredict =
    selectedPlayer && playerTeam && opponent && statLine && parseFloat(statLine) > 0 && statType

  const handlePredict = async () => {
    if (!canPredict) return
    setPredicting(true)
    setError('')
    setResult(null)
    try {
      const res = await apiFetch('/predict', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          player_name: selectedPlayer,
          opponent_team: opponent,
          stat_line: parseFloat(statLine),
          stat_type: statType,
          is_home: isHome,
          is_back_to_back: isBackToBack,
          game_date: gameDate || null,
          league: sport,
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

  const themeLabel = theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'
  const themeButton = (withText) => (
    <button
      onClick={toggleTheme}
      className="icon-btn tap flex items-center gap-2 px-2 py-1.5 text-sm"
      aria-label={themeLabel}
      title={themeLabel}
    >
      {theme === 'dark' ? <SunIcon /> : <MoonIcon />}
      {withText && <span className="hidden lg:inline">{theme === 'dark' ? 'Light mode' : 'Dark mode'}</span>}
    </button>
  )

  // Logged out, checking or offline: the product's name over one centred card.
  if (!loggedIn) {
    return (
      <div className="app-shell min-h-screen flex flex-col items-center px-4 pt-6 pb-12">
        <div className="w-full max-w-sm flex justify-end">{themeButton(false)}</div>
        <div className="w-full max-w-sm mt-10 sm:mt-16 mb-6 flex items-center gap-3">
          <Mark size={28} />
          <div>
            <p className="text-base font-semibold leading-tight">Sports Bettr</p>
            <p className="text-sm" style={{ color: 'var(--text-muted)' }}>Over/under predictions from real game data</p>
          </div>
        </div>

        {auth === 'checking' && (
          <div className="w-full max-w-sm flex items-center gap-2 py-6 text-sm" style={{ color: 'var(--text-muted)' }}>
            <Spinner /> Loading…
          </div>
        )}

        {auth === 'offline' && (
          <div className="card w-full max-w-sm p-6 space-y-4">
            <p className="text-sm">Can’t reach the server.</p>
            <button onClick={recheck} className="btn-ghost tap w-full py-2.5 text-sm font-medium">Try again</button>
          </div>
        )}

        {(auth === 'out' || auth === 'expired') && <Login expired={auth === 'expired'} onLoggedIn={() => setAuth('in')} />}
      </div>
    )
  }

  // One navigation element for every width: a labelled sidebar from 1024 px, a top bar below.
  return (
    <div className={`min-h-screen lg:flex ${currentPage === 'history' ? 'shell-fit' : ''}`}>
      <nav aria-label="Main"
           className="shell-nav app-topbar shrink-0 flex items-center gap-1 px-3 sm:px-6 h-14 border-b
                      lg:sticky lg:top-0 lg:h-screen lg:w-52 lg:flex-col lg:items-stretch lg:gap-1 lg:px-3 lg:py-5
                      lg:border-b-0 lg:border-r">
        <div className="flex items-center gap-2 mr-2 lg:mr-0 lg:px-2 lg:mb-6">
          <Mark />
          <span className="hidden min-[400px]:inline text-sm font-semibold whitespace-nowrap">Sports Bettr</span>
        </div>
        {['predict', 'history'].map((page) => (
          <button
            key={page}
            onClick={() => setCurrentPage(page)}
            className="choice tap px-2 py-1.5 text-sm font-medium lg:text-left"
            aria-current={currentPage === page ? 'page' : undefined}
          >
            <span className="choice-label">{page === 'predict' ? 'Predict' : 'History'}</span>
          </button>
        ))}
        <div className="ml-auto flex items-center gap-1 lg:ml-0 lg:mt-auto lg:flex-col lg:items-stretch">
          {themeButton(true)}
          <button
            onClick={handleLogout}
            className="icon-btn tap flex items-center gap-2 px-2 py-1.5 text-sm"
            aria-label="Log out"
            title="Log out"
          >
            <LogoutIcon />
            <span className="hidden lg:inline">Log out</span>
          </button>
        </div>
      </nav>

      <main className="app-shell flex-1 min-w-0 px-4 sm:px-6 lg:px-8 pt-6 lg:pt-8 pb-12">
        <div className="shell-content mx-auto w-full max-w-[1200px]">
          {currentPage === 'history' && <History />}

          {currentPage === 'predict' && <>
            <h1 className="text-2xl font-semibold tracking-tight mb-6">Predict</h1>
            <div className="space-y-6">
              {/* Main entry: screenshot -> confirm -> predict (manual form below) */}
              {sport === 'NBA' && <ScreenshotImport teams={allTeams} />}

              <div className="grid gap-6 lg:grid-cols-2 items-start">
                <div className="space-y-6 min-w-0">
                  {/* Form card */}
                  <section className="card p-5 space-y-4" aria-labelledby="single-prop">
                    <div className="flex items-center justify-between gap-3">
                      <h2 id="single-prop" className="panel-title">Single prop</h2>
                      <div className="flex gap-1">
                        {['NBA', 'WNBA'].map((s) => (
                          <Pill key={s} active={sport === s} onClick={() => handleSportChange(s)}>{s}</Pill>
                        ))}
                      </div>
                    </div>

                    {wnbaOff ? (
                      <div className="notice px-3 py-2 text-sm">
                        WNBA is turned off on this server.
                      </div>
                    ) : (
                      <PlayerSearch
                        value={playerQuery}
                        onChange={setPlayerQuery}
                        onSelect={handlePlayerSelect}
                        onClear={resetAll}
                        searchPath={`${sportPrefix}/players/search`}
                        placeholder={`Search ${sport} players…`}
                      />
                    )}

                    {playerContext?.injury_status && (
                      <InjuryBanner
                        status={playerContext.injury_status.status}
                        reason={playerContext.injury_status.reason}
                      />
                    )}
                    {playerContext?.news && <NewsNote text={playerContext.news} />}

                    <FieldWrapper visible={showAdditionalFields}>
                      <div className="grid gap-4 sm:grid-cols-2">
                        <TeamField team={playerTeam} onClear={resetAll} />
                        <OpponentField
                          value={opponent}
                          teams={allTeams}
                          playerTeam={playerTeam}
                          onChange={setOpponent}
                          onClear={() => { setOpponent(''); setResult(null) }}
                        />
                        <StatTypeField
                          value={statType}
                          onChange={setStatType}
                          onClear={() => { setStatType(''); setResult(null) }}
                        />
                        <StatLineField
                          value={statLine}
                          onChange={(val) => { setStatLine(val); setLineSource(null) }}
                          onClear={() => { setStatLine(''); setLineSource(null); setResult(null) }}
                          source={lineSource}
                          lineLoading={lineLoading}
                        />
                        <GameDateField value={gameDate} onChange={setGameDate} />
                      </div>
                    </FieldWrapper>

                    <FieldWrapper visible={showAdditionalFields}>
                      <button
                        onClick={handlePredict}
                        disabled={!canPredict || predicting}
                        className="btn-accent tap w-full py-2.5 text-sm"
                      >
                        {predicting ? (
                          <span className="flex items-center justify-center gap-2">
                            <Spinner />
                            Analyzing…
                          </span>
                        ) : (
                          'Predict'
                        )}
                      </button>
                    </FieldWrapper>

                    {error && (
                      <div role="alert" className="notice animate-fade-in px-3 py-2 text-sm">
                        {error}
                      </div>
                    )}
                  </section>

                  {result && <>
                    <ResultCard result={result} player={selectedPlayer} stat={statType} league={sport} />
                    <OpponentInjuryReport injuries={opponentInjuries} opponentName={opponent} />
                    <button
                      onClick={resetAll}
                      className="btn-ghost tap w-full py-2.5 text-sm font-medium"
                    >
                      Clear all
                    </button>
                  </>}
                </div>

                {sport === 'NBA' && (
                  <section className="card p-5 min-w-0" aria-labelledby="slip-title">
                    <h2 id="slip-title" className="panel-title">Slip</h2>
                    <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2">
                      <span className="text-sm" style={{ color: 'var(--text-muted)' }}>Legs</span>
                      <div className="flex gap-1">
                        {[2, 3, 4, 5].map((n) => (
                          <Pill key={n} active={slipSize === n} onClick={() => setSlipSize(n)}>{n}</Pill>
                        ))}
                      </div>
                      <button
                        onClick={handleBuildSlip}
                        disabled={slipLoading}
                        className="btn-ghost tap ml-auto px-4 py-2 text-sm font-medium"
                      >
                        {slipLoading ? 'Building your slip…' : 'Build me a slip'}
                      </button>
                    </div>

                    <SlipCard slip={slipResult} onClear={() => setSlipResult(null)} />
                  </section>
                )}
              </div>
            </div>
          </>}
        </div>
      </main>
    </div>
  )
}
