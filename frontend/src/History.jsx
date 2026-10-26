import { useState, useEffect, useCallback, useRef } from 'react'
import { fetchTrackRecord, pct, rangeStr, TRACK_MODEL } from './trackRecord'
import { duplicateCount, historySummary } from './historyStats'
import ConfirmDialog from './components/ConfirmDialog'
import { apiFetch } from './api'
import { formatCentral, formatGameDate } from './format'
import { useIsPhone } from './useMediaQuery'

const PAGE_SIZES = [10, 25, 50]

const ALL_STAT_TYPES = [
  'All', 'Points', 'Assists', 'Rebounds', 'PRA', '3PM', 'PR', 'PA', 'RA',
  'Blocks', 'Steals', 'Blocks+Steals', 'Turnovers',
  'Offensive Rebounds', 'Defensive Rebounds', 'Double Double', '3PA',
]
const OUTCOME_FILTERS = ['All', 'Correct', 'Incorrect', 'Pending', 'DNP']
const LEAGUE_FILTERS = ['All', 'NBA', 'NFL', 'WNBA']
// Which model made the pick (predictions.model). Stats and table default to the challenger.
// 'Challenger retro' rows re-score frozen picks after the fact (exploratory); 'All' is the
// live picks only (challenger + frozen), so a retro row never double-counts a game.
const MODEL_FILTERS = ['Challenger', 'Frozen', 'Challenger retro', 'All']
const MODEL_KEYS = { Challenger: ['challenger'], Frozen: ['frozen'], 'Challenger retro': ['challenger_retro'],
                     All: ['challenger', 'frozen'] }
const TOOLTIP_WIDTH = 260

// "12 rows", or "335 current · 337 total" when replaced (dimmed) picks are among them
export function rowCountLabel(rows) {
  const current = rows.filter((p) => p.is_current !== 0).length
  return current === rows.length
    ? `${rows.length} row${rows.length !== 1 ? 's' : ''}`
    : `${current} current · ${rows.length} total`
}

// ---------------------------------------------------------------------------
// Summary cards
// ---------------------------------------------------------------------------

function SummaryCard({ label, value, sub }) {
  return (
    <div className="card rounded-2xl px-4 py-3 flex flex-col gap-0.5">
      <span className="text-xs uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>{label}</span>
      <span className="text-2xl font-bold" style={{ color: 'var(--text-primary)' }}>{value}</span>
      {sub && <span className="text-xs" style={{ color: 'var(--text-muted)' }}>{sub}</span>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Filter select
// ---------------------------------------------------------------------------

// On phones each filter has its label above it (three of them otherwise just read "All");
// on desktop they sit in one row after "Filter:", as before.
function FilterSelect({ value, onChange, options, label, className = '' }) {
  return (
    <label className="flex flex-col gap-1 sm:contents">
      <span className="sm:hidden text-[11px] font-semibold uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>
        {label}
      </span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label={label}
        className={`input-field rounded-xl px-3 py-2 text-sm cursor-pointer ${className}`}
      >
        {options.map((o) => (
          <option key={o} value={o}>{o}</option>
        ))}
      </select>
    </label>
  )
}

// ---------------------------------------------------------------------------
// Prediction row
// ---------------------------------------------------------------------------

// Player name with a tooltip: the app's track record on this player and stat for the
// model filter's choice, over the rows the stats cards count (current, not limited-data,
// resolved). Opens on hover (desktop) or tap (phones; a second tap or a tap elsewhere closes
// it). Fixed-positioned and kept on screen, so a truncating cell can't clip it.
function PlayerName({ name, stat, league, model }) {
  const [tip, setTip] = useState(null)   // { x, y, tr, error }
  const openedAt = useRef(0)
  const ref = useRef(null)

  const show = () => {
    const r = ref.current.getBoundingClientRect()
    const x = Math.max(8, Math.min(r.left, window.innerWidth - TOOLTIP_WIDTH - 8))
    openedAt.current = Date.now()
    setTip({ x, y: r.bottom + 6, tr: null })
    fetchTrackRecord({ player: name, stat, league, model })
      .then((tr) => setTip((t) => (t ? { ...t, tr } : t)))
      .catch(() => setTip((t) => (t ? { ...t, error: true } : t)))
  }

  // A touch fires mouseenter just before click: a click right after opening keeps it open.
  const toggle = () => {
    if (tip && Date.now() - openedAt.current > 400) setTip(null)
    else if (!tip) show()
  }

  // After a touch, the browser's emulated mouse events (mouseenter/mouseleave, which can land on
  // a neighbouring element once the page has scrolled) are ignored: taps alone open and close
  // it. A real mouse moving over the name switches hover back on.
  const touch = useRef(false)
  const onMouseEnter = () => { if (!touch.current) show() }
  const onMouseLeave = () => { if (!touch.current) setTip(null) }

  // Closes on a tap elsewhere, a real page scroll (more than a small jitter: a phone's viewport
  // shifts a little around a tap), or a scroll inside a container (the table), which would
  // leave the fixed-position tooltip pointing at the wrong row.
  const open = !!tip
  useEffect(() => {
    if (!open) return undefined
    const y0 = window.scrollY
    const close = (e) => { if (!ref.current?.contains(e.target)) setTip(null) }
    const onScroll = (e) => {
      const page = e.target === document || e.target === document.documentElement
      if (!page || Math.abs(window.scrollY - y0) > 24) setTip(null)
    }
    document.addEventListener('pointerdown', close)
    window.addEventListener('scroll', onScroll, true)
    return () => {
      document.removeEventListener('pointerdown', close)
      window.removeEventListener('scroll', onScroll, true)
    }
  }, [open])

  return (
    <>
      <span ref={ref} role="button" tabIndex={0} onMouseEnter={onMouseEnter} onMouseLeave={onMouseLeave} onClick={toggle}
            onPointerDown={(e) => { touch.current = e.pointerType !== 'mouse' }}
            onPointerMove={(e) => { if (e.pointerType === 'mouse') touch.current = false }}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle() } }}
            className="cursor-help tap-inline underline decoration-dotted underline-offset-4 sm:no-underline"
            style={{ textDecorationColor: 'var(--text-muted)' }}>{name}</span>
      {tip && (
        <div
          role="tooltip"
          className="fixed z-50 rounded-xl px-3 py-2 text-xs space-y-0.5 shadow-lg pointer-events-none"
          style={{ left: tip.x, top: tip.y, maxWidth: TOOLTIP_WIDTH, backgroundColor: 'var(--bg-secondary)', border: '1px solid var(--border)', color: 'var(--text-secondary)' }}
        >
          <div className="font-semibold" style={{ color: 'var(--text-primary)' }}>{name} · {stat}</div>
          {tip.error ? (
            <div style={{ color: 'var(--text-muted)' }}>Track record unavailable</div>
          ) : !tip.tr ? (
            <div style={{ color: 'var(--text-muted)' }}>Loading…</div>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <span>Record: {tip.tr.wins}–{tip.tr.losses} ({tip.tr.n} picks)</span>
                {!tip.tr.enough && (
                  <span className="rounded px-1.5 py-0.5 text-[10px] font-semibold"
                        style={{ backgroundColor: 'var(--warning-bg)', color: 'var(--warning)', border: '1px solid var(--warning-border)' }}>
                    Small sample
                  </span>
                )}
              </div>
              <div>Raw: {pct(tip.tr.raw_rate)} ({rangeStr(tip.tr)})</div>
              <div>Adjusted: {pct(tip.tr.adjusted_rate)}</div>
              <div style={{ color: 'var(--text-muted)' }}>All players, {stat}: {pct(tip.tr.overall_rate)} · resolved picks only</div>
            </>
          )}
        </div>
      )}
    </>
  )
}

// What a row shows, shared by the table (desktop) and the cards (phones)
function rowView(p) {
  const isOver = p.predicted_outcome === 'OVER'
  const isDNP = p.actual_result === 'DNP'
  const isResolved = p.actual_result !== null && !isDNP
  const isCorrect = isResolved && p.predicted_outcome === p.actual_result
  const resultIcon = isDNP
    ? <span style={{ color: 'var(--text-muted)' }}>—</span>
    : isResolved ? (isCorrect ? '✅' : '❌') : '⏳'
  const actualDisplay = isDNP
    ? <span className="font-semibold" style={{ color: '#8b5cf6' }}>DNP</span>
    : isResolved
    ? <span className="font-semibold" style={{ color: p.actual_result === 'OVER' ? 'var(--success)' : 'var(--danger)' }}>{p.actual_result}</span>
    : <span style={{ color: 'var(--text-muted)' }}>Pending</span>
  const predicted = (
    <span className="font-semibold" style={{ color: isOver ? 'var(--success)' : 'var(--danger)' }}>
      {p.predicted_outcome}
    </span>
  )
  return { resultIcon, actualDisplay, predicted, dateStr: formatCentral(p.timestamp),
           tableDateStr: formatCentral(p.timestamp, { suffix: false }), gameDateStr: formatGameDate(p.game_date) }
}

const LIMITED_TITLE = 'Rookie or fewer than 10 games before this game: not counted in the accuracy figures'

function LimitedBadge() {
  return (
    <span className="ml-1.5 rounded px-1 py-0.5 text-[10px] font-medium whitespace-nowrap"
          style={{ backgroundColor: 'var(--bg-tertiary)', color: 'var(--text-muted)' }} title={LIMITED_TITLE}>
      limited data
    </span>
  )
}

// Set the actual result by hand: ✏ opens OVER / UNDER with confirm and cancel.
function ResultEditor({ isEditing, editValue, onEdit, onEditChange, onEditConfirm, onEditCancel, large = false }) {
  const size = large ? 'tap' : ''
  if (!isEditing) {
    return <button onClick={onEdit} className={`icon-btn ${size} text-sm px-1 flex items-center justify-center`}
                   title="Set actual result" aria-label="Set actual result">✏</button>
  }
  return (
    <div className="flex items-center gap-1.5">
      <select value={editValue} onChange={(e) => onEditChange(e.target.value)} aria-label="Actual result"
              className={`input-field rounded-lg px-2 py-1 text-xs ${size}`}>
        <option value="OVER">OVER</option>
        <option value="UNDER">UNDER</option>
      </select>
      <button onClick={onEditConfirm} className={`btn-accent ${size} text-xs px-2 py-1 rounded-lg`} aria-label="Save">✓</button>
      <button onClick={onEditCancel} className={`icon-btn ${size} text-xs px-1 py-1`} aria-label="Cancel">✕</button>
    </div>
  )
}

function PredictionRow({ prediction: p, editor, stripe, model }) {
  const v = rowView(p)
  return (
    <tr style={{ backgroundColor: stripe ? 'var(--bg-stripe)' : 'transparent', opacity: p.is_current === 0 ? 0.45 : 1 }}>
      <td className="px-2 py-2 text-xs truncate" style={{ color: 'var(--text-muted)' }}>{v.tableDateStr}</td>
      <td className="px-2 py-2 text-xs truncate" style={{ color: 'var(--text-muted)' }}>{v.gameDateStr}</td>
      <td className="px-2 py-2 truncate" style={{ color: 'var(--text-secondary)' }}>
        <PlayerName name={p.player_name} stat={p.stat_type} league={p.league ?? 'NBA'} model={model} />
        {!!p.is_limited_data && <LimitedBadge />}
      </td>
      <td className="px-2 py-2 truncate" style={{ color: 'var(--text-muted)' }}>{p.stat_type}</td>
      <td className="px-2 py-2 truncate" style={{ color: 'var(--text-secondary)' }}>{p.stat_line}</td>
      <td className="px-2 py-2 truncate" style={{ color: 'var(--text-muted)' }}>{p.opponent_team}</td>
      <td className="px-2 py-2 truncate">{v.predicted}</td>
      <td className="px-2 py-2 truncate" style={{ color: 'var(--text-secondary)' }}>{(p.confidence * 100).toFixed(1)}%</td>
      <td className="px-2 py-2 truncate">{v.actualDisplay}</td>
      <td className="px-2 py-2 text-center">{v.resultIcon}</td>
      <td className="px-2 py-2"><ResultEditor {...editor} /></td>
    </tr>
  )
}

// Phones: one card per prediction instead of an 11-column row.
function PredictionCard({ prediction: p, editor, model }) {
  const v = rowView(p)
  return (
    <li className="card rounded-2xl px-4 py-3 space-y-1.5" style={{ opacity: p.is_current === 0 ? 0.45 : 1 }}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 text-sm font-semibold" style={{ color: 'var(--text-primary)' }}>
          <PlayerName name={p.player_name} stat={p.stat_type} league={p.league ?? 'NBA'} model={model} />
          {!!p.is_limited_data && <LimitedBadge />}
        </div>
        <span className="shrink-0 text-base leading-none">{v.resultIcon}</span>
      </div>
      <div className="text-sm" style={{ color: 'var(--text-secondary)' }}>
        {p.stat_type} {p.stat_line} · vs {p.opponent_team}
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
        <span>{v.predicted} <span style={{ color: 'var(--text-muted)' }}>{(p.confidence * 100).toFixed(1)}%</span></span>
        <span style={{ color: 'var(--text-muted)' }}>Actual: {v.actualDisplay}</span>
      </div>
      <div className="flex items-center justify-between gap-3">
        <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
          Game {v.gameDateStr} · made {v.dateStr}
        </span>
        <ResultEditor {...editor} large />
      </div>
    </li>
  )
}

// ---------------------------------------------------------------------------
// Main History component
// ---------------------------------------------------------------------------

export default function History() {
  const [predictions, setPredictions] = useState([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState(false)
  const [cleaning, setCleaning] = useState(false)
  const [statFilter, setStatFilter] = useState('All')
  const [outcomeFilter, setOutcomeFilter] = useState('All')
  const [leagueFilter, setLeagueFilter] = useState('All')
  const [modelFilter, setModelFilter] = useState('Challenger')
  const [searchQuery, setSearchQuery] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [editingId, setEditingId] = useState(null)
  const [editValue, setEditValue] = useState('OVER')
  const [sortConfig, setSortConfig] = useState({ key: null, direction: 'asc' })
  const [confirmClean, setConfirmClean] = useState(null)    // rows the clean-up would remove, while asking
  const isPhone = useIsPhone()

  const fetchHistory = useCallback(async () => {
    setLoading(true)
    setLoadError(false)
    try {
      const res = await apiFetch('/predictions/history')
      if (res.ok === false) throw new Error(`history ${res.status}`)
      const data = await res.json()
      setPredictions(data)
    } catch {
      setPredictions([])
      setLoadError(true)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchHistory() }, [fetchHistory])

  // Reset to page 1 whenever filters, search, page size, or sort change
  useEffect(() => { setPage(1) }, [statFilter, outcomeFilter, leagueFilter, modelFilter, searchQuery, pageSize, sortConfig])

  const handleOverride = async (id) => {
    try {
      await apiFetch(`/predictions/${id}/result`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ actual_result: editValue }),
      })
      setEditingId(null)
      fetchHistory()
    } catch {}
  }

  const handleSort = (key) => {
    setSortConfig((prev) => {
      if (prev.key !== key) return { key, direction: 'asc' }
      if (prev.direction === 'asc') return { key, direction: 'desc' }
      return { key: null, direction: 'asc' }
    })
  }

  // Ask first: how many rows the clean-up would remove (counted from the history already
  // loaded, with the backend's rule); nothing is deleted unless confirmed.
  const askCleanDuplicates = () => setConfirmClean(duplicateCount(predictions))

  const handleCleanDuplicates = async () => {
    setConfirmClean(null)
    setCleaning(true)
    try {
      await apiFetch('/predictions/duplicates', { method: 'DELETE' })
      fetchHistory()
    } catch {}
    finally { setCleaning(false) }
  }

  // Summary figures over the selected model's predictions (historyStats: DNP and
  // limited-data rows excluded)
  const byModel = predictions.filter((p) => MODEL_KEYS[modelFilter].includes(p.model ?? 'frozen'))
  const summary = historySummary(byModel)

  // Filter pipeline
  const filtered = byModel.filter((p) => {
    if (leagueFilter !== 'All' && (p.league ?? 'NBA') !== leagueFilter) return false
    if (statFilter !== 'All' && p.stat_type !== statFilter) return false
    if (outcomeFilter === 'Pending') return p.actual_result === null
    if (outcomeFilter === 'DNP') return p.actual_result === 'DNP'
    if (outcomeFilter === 'Correct') return p.actual_result !== null && p.actual_result !== 'DNP' && p.predicted_outcome === p.actual_result
    if (outcomeFilter === 'Incorrect') return p.actual_result !== null && p.actual_result !== 'DNP' && p.predicted_outcome !== p.actual_result
    if (searchQuery && !p.player_name.toLowerCase().includes(searchQuery.toLowerCase())) return false
    return true
  })

  // Sort (after filter, before pagination)
  const NUMERIC_SORT_KEYS = new Set(['stat_line', 'confidence'])
  const sorted = sortConfig.key
    ? [...filtered].sort((a, b) => {
        let aVal = a[sortConfig.key]
        let bVal = b[sortConfig.key]
        if (aVal === null && bVal === null) return 0
        if (aVal === null) return 1
        if (bVal === null) return -1
        const cmp = NUMERIC_SORT_KEYS.has(sortConfig.key)
          ? aVal - bVal
          : String(aVal).localeCompare(String(bVal))
        return sortConfig.direction === 'asc' ? cmp : -cmp
      })
    : filtered

  // Pagination
  const totalPages = Math.max(1, Math.ceil(sorted.length / pageSize))
  const clampedPage = Math.min(page, totalPages)
  const paginated = sorted.slice((clampedPage - 1) * pageSize, clampedPage * pageSize)

  const hasActiveFilter = statFilter !== 'All' || outcomeFilter !== 'All' || leagueFilter !== 'All' || searchQuery !== ''

  const editorFor = (p) => ({
    isEditing: editingId === p.id,
    editValue,
    onEdit: () => { setEditingId(p.id); setEditValue('OVER') },
    onEditChange: setEditValue,
    onEditConfirm: () => handleOverride(p.id),
    onEditCancel: () => setEditingId(null),
  })

  const SORT_COLUMNS = [
    { label: 'Date (CT)', key: 'timestamp' },
    { label: 'Game Date', key: 'game_date' },
    { label: 'Player',    key: 'player_name' },
    { label: 'Stat',      key: 'stat_type' },
    { label: 'Line',      key: 'stat_line' },
    { label: 'Opponent',  key: 'opponent_team' },
    { label: 'Predicted', key: 'predicted_outcome' },
    { label: 'Conf.',     key: 'confidence' },
    { label: 'Actual',    key: 'actual_result' },
  ]

  return (
    <div className="w-full max-w-6xl mx-auto sm:px-4">
      {/* Header row: summary cards + clean button */}
      <div className="flex flex-col sm:flex-row items-stretch sm:items-start gap-3 sm:gap-4 mb-6">
        <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 flex-1">
          <SummaryCard label="Current picks" value={summary.total}
                       sub={`${summary.resolved} resolved${summary.all !== summary.total ? ` · ${summary.all} rows in all` : ''}`} />
          <SummaryCard label="Accuracy" value={summary.accuracy} sub={summary.resolved > 0 ? `${summary.correct}/${summary.resolved} correct` : 'no data'} />
          {summary.tiers.map((t) => (
            <SummaryCard key={t.label} label={t.label} value={t.value} sub={t.sub} />
          ))}
        </div>
        <div className="shrink-0 sm:pt-1">
          <button
            onClick={askCleanDuplicates}
            disabled={cleaning || loading}
            className="btn-danger-ghost tap w-full sm:w-auto px-3 py-2 rounded-xl text-xs"
            title="Remove duplicates, keeping earliest per unique combo"
          >
            {cleaning ? 'Cleaning…' : 'Clean Duplicates'}
          </button>
        </div>
      </div>

      {(summary.limitedCount > 0 || summary.all > summary.current) && (
        <p className="text-xs -mt-3 sm:-mt-4 mb-4" style={{ color: 'var(--text-muted)' }}>
          {summary.all > summary.current &&
            `${summary.all - summary.current} replaced pick${summary.all - summary.current !== 1 ? 's' : ''} (dimmed: a later pick for the same player, stat and game) `}
          {summary.limitedCount > 0 &&
            `${summary.all > summary.current ? 'and ' : ''}${summary.limitedCount} limited-data prediction${summary.limitedCount !== 1 ? 's' : ''} `}
          not counted in these figures.
        </p>
      )}

      {confirmClean !== null && (
        <ConfirmDialog
          title="Clean duplicates?"
          message={confirmClean > 0
            ? `This permanently removes ${confirmClean} duplicate prediction${confirmClean !== 1 ? 's' : ''}: for each player, stat, line, opponent, game date and model it keeps only the newest pick.`
            : 'There are no duplicate predictions to remove.'}
          confirmLabel={confirmClean > 0 ? `Remove ${confirmClean}` : null}
          danger
          onConfirm={handleCleanDuplicates}
          onCancel={() => setConfirmClean(null)}
        />
      )}

      {/* Filters row: a two-column grid on phones, one row on desktop */}
      <div className="grid grid-cols-2 sm:flex sm:flex-wrap items-center gap-2 mb-4">
        <span className="hidden sm:inline text-xs uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>Filter:</span>
        <FilterSelect value={modelFilter} onChange={setModelFilter} options={MODEL_FILTERS} label="Model" className="w-full sm:w-auto" />
        <FilterSelect value={leagueFilter} onChange={setLeagueFilter} options={LEAGUE_FILTERS} label="League" className="w-full sm:w-auto" />
        <FilterSelect value={statFilter} onChange={setStatFilter} options={ALL_STAT_TYPES} label="Stat" className="w-full sm:w-auto" />
        <FilterSelect value={outcomeFilter} onChange={setOutcomeFilter} options={OUTCOME_FILTERS} label="Outcome" className="w-full sm:w-auto" />
        <input
          type="search"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="Search player…"
          aria-label="Search player"
          className="input-field rounded-xl px-3 py-2 text-sm w-full sm:w-40 col-span-2"
        />
        {hasActiveFilter && (
          <button
            onClick={() => { setLeagueFilter('All'); setStatFilter('All'); setOutcomeFilter('All'); setSearchQuery('') }}
            className="icon-btn tap text-xs justify-self-start"
          >
            Clear filters
          </button>
        )}
        <div className="col-span-2 sm:ml-auto flex items-center justify-end gap-2">
          <span className="text-xs" style={{ color: 'var(--text-muted)' }}>Rows per page:</span>
          <select
            value={pageSize}
            onChange={(e) => setPageSize(Number(e.target.value))}
            aria-label="Rows per page"
            className="input-field rounded-xl px-2 py-2 text-sm cursor-pointer"
          >
            {PAGE_SIZES.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          <span className="text-xs" style={{ color: 'var(--text-muted)' }}>{rowCountLabel(filtered)}</span>
        </div>
      </div>

      {/* Table (desktop) or cards (phones) */}
      {loading ? (
        <div className="text-center py-16" style={{ color: 'var(--text-muted)' }}>Loading…</div>
      ) : loadError ? (
        <div className="text-center py-16 space-y-3" style={{ color: 'var(--text-muted)' }}>
          <p>Couldn’t load the history.</p>
          <button onClick={fetchHistory} className="btn-ghost tap px-4 py-2 rounded-xl text-sm">Try again</button>
        </div>
      ) : filtered.length === 0 ? (
        <div className="text-center py-16" style={{ color: 'var(--text-muted)' }}>
          {predictions.length === 0 ? 'No predictions logged yet.' : 'No predictions match the current filters.'}
        </div>
      ) : (
        <>
          {isPhone ? (
            <ul className="space-y-2" aria-label="Predictions">
              {paginated.map((p) => (
                <PredictionCard key={p.id} prediction={p} editor={editorFor(p)} model={TRACK_MODEL[modelFilter]} />
              ))}
            </ul>
          ) : (
            <div
              className="overflow-y-auto max-h-96 rounded-2xl border"
              style={{ borderColor: 'var(--border)' }}
            >
              <table className="w-full text-sm" style={{ tableLayout: 'fixed' }}>
                <colgroup>
                  <col style={{ width: '13%' }} />
                  <col style={{ width: '8%' }} />
                  <col style={{ width: '13%' }} />
                  <col style={{ width: '9%' }} />
                  <col style={{ width: '5%' }} />
                  <col style={{ width: '14%' }} />
                  <col style={{ width: '7%' }} />
                  <col style={{ width: '6%' }} />
                  <col style={{ width: '7%' }} />
                  <col style={{ width: '5%' }} />
                  <col style={{ width: '3%' }} />
                </colgroup>
                <thead>
                  <tr
                    className="text-left text-xs uppercase tracking-widest border-b"
                    style={{ borderBottomColor: 'var(--border)' }}
                  >
                    {SORT_COLUMNS.map(({ label, key }) => {
                      const active = sortConfig.key === key
                      return (
                        <th
                          key={key}
                          onClick={() => handleSort(key)}
                          className="sort-header px-2 py-2 font-semibold truncate"
                        >
                          {label}
                          {active && (
                            <span className="ml-1" style={{ color: 'var(--accent)' }}>
                              {sortConfig.direction === 'asc' ? '↑' : '↓'}
                            </span>
                          )}
                        </th>
                      )
                    })}
                    <th className="px-2 py-2 font-semibold text-center select-none" style={{ color: 'var(--text-muted)' }}>Result</th>
                    <th className="px-2 py-2"></th>
                  </tr>
                </thead>
                <tbody className="divide-theme">
                  {paginated.map((p, i) => (
                    <PredictionRow
                      key={p.id}
                      prediction={p}
                      editor={editorFor(p)}
                      stripe={i % 2 === 1}
                      model={TRACK_MODEL[modelFilter]}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between gap-2 mt-4 px-1">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={clampedPage === 1}
                className="btn-ghost tap px-4 py-2 rounded-xl text-sm"
              >
                ← Previous
              </button>
              <span className="text-xs text-center" style={{ color: 'var(--text-muted)' }}>
                Page {clampedPage} of {totalPages}
              </span>
              <button
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={clampedPage === totalPages}
                className="btn-ghost tap px-4 py-2 rounded-xl text-sm"
              >
                Next →
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
