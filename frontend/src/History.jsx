import { useState, useEffect, useCallback, useRef } from 'react'
import { fetchTrackRecord, pct, rangeStr, TRACK_MODEL } from './trackRecord'
import { duplicateCount, historySummary } from './historyStats'
import ConfirmDialog from './components/ConfirmDialog'
import { apiFetch } from './api'
import { formatCentral, formatGameDate } from './format'
import { useIsPhone } from './useMediaQuery'
import { sideColor } from './sides'

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

// The phone list has no column headers to click: with it (and only with it: the same isPhone switch)
// a "Sort by" select offers three sorts on the table's sort state. "key:direction"; '' is the
// default order (newest made first), which a sort on Made, newest first equals. "Actual" is its
// own order (actual_graded: OVER, UNDER, DNP, then Pending), which no column header gives.
const SORT_OPTIONS = [
  ['', 'Newest'],
  ['confidence:desc', 'Confidence'],
  ['actual_graded:asc', 'Actual'],
]
// A sort set from a desktop header that the phone doesn't offer is shown under its usual label
// (an extra option) until another is picked, so the select never names the wrong order.
const HEADER_SORT_LABELS = {
  'timestamp:asc': 'Made, oldest first',
  'game_date:desc': 'Game date, latest first',
  'game_date:asc': 'Game date, earliest first',
  'player_name:asc': 'Player, A to Z',
  'player_name:desc': 'Player, Z to A',
  'confidence:asc': 'Confidence, low to high',
  'stat_line:desc': 'Line, high to low',
  'stat_line:asc': 'Line, low to high',
  'stat_type:asc': 'Stat, A to Z',
  'stat_type:desc': 'Stat, Z to A',
  'opponent_team:asc': 'Opponent, A to Z',
  'opponent_team:desc': 'Opponent, Z to A',
  'predicted_outcome:asc': 'Predicted, OVER first',
  'predicted_outcome:desc': 'Predicted, UNDER first',
  'actual_result:asc': 'Actual, DNP then OVER then UNDER',
  'actual_result:desc': 'Actual, UNDER then OVER then DNP',
}
export function sortSelectOptions(value) {
  if (SORT_OPTIONS.some(([v]) => v === value)) return SORT_OPTIONS
  return [...SORT_OPTIONS, [value, HEADER_SORT_LABELS[value] ?? value]]
}
const ACTUAL_GRADED_RANK = { OVER: 0, UNDER: 1, DNP: 2 }   // Pending (null) after all of them

// The select's value for a sort state, and the sort state for a select value
export function sortOptionValue({ key, direction }) {
  if (!key || (key === 'timestamp' && direction === 'desc')) return ''
  return `${key}:${direction}`
}
export function sortFromOption(value) {
  if (!value) return { key: null, direction: 'asc' }
  const [key, direction] = value.split(':')
  return { key, direction }
}

// "12 rows", or "335 current · 337 total" when replaced (dimmed) picks are among them
export function rowCountLabel(rows) {
  const current = rows.filter((p) => p.is_current !== 0).length
  return current === rows.length
    ? `${rows.length} row${rows.length !== 1 ? 's' : ''}`
    : `${current} current · ${rows.length} total`
}

// ---------------------------------------------------------------------------
// Summary strip
// ---------------------------------------------------------------------------

// One cell of the summary strip: a grey label over the figure, a grey note under it.
function SummaryCell({ label, value, sub, lead = false }) {
  return (
    <div className="summary-cell px-4 py-3 min-w-0">
      <div className="text-xs" style={{ color: 'var(--text-muted)' }}>{label}</div>
      <div className={`num ${lead ? 'text-[30px] leading-9 font-semibold' : 'text-2xl leading-9'}`}>{value}</div>
      {sub && <div className="text-xs num" style={{ color: 'var(--text-muted)' }}>{sub}</div>}
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
    <label className="flex flex-col">
      <span className="field-label">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label={label}
        className={`input-field h-[34px] px-2.5 text-sm cursor-pointer ${className}`}
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
            className="cursor-help tap-inline underline decoration-dotted underline-offset-4"
            style={{ textDecorationColor: 'var(--control)' }}>{name}</span>
      {tip && (
        <div
          role="tooltip"
          className="floating fixed z-50 rounded-md px-3 py-2 text-xs space-y-0.5 pointer-events-none num"
          style={{ left: tip.x, top: tip.y, maxWidth: TOOLTIP_WIDTH }}
        >
          <div className="font-semibold">{name} · {stat}</div>
          {tip.error ? (
            <div style={{ color: 'var(--text-muted)' }}>Track record unavailable</div>
          ) : !tip.tr ? (
            <div style={{ color: 'var(--text-muted)' }}>Loading…</div>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <span>Record: {tip.tr.wins}–{tip.tr.losses} ({tip.tr.n} picks)</span>
                {!tip.tr.enough && (
                  <span className="tag">Small sample</span>
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

// What a row shows, shared by the table (desktop) and the list (phones). OVER and UNDER each
// have a hue (--over, --under) on the pick and on where the result landed (the history carries
// OVER / UNDER, not the box-score number); Pending and DNP stay grey; Hit / Miss keep their tags.

function rowView(p) {
  const isDNP = p.actual_result === 'DNP'
  const isResolved = p.actual_result !== null && !isDNP
  const isCorrect = isResolved && p.predicted_outcome === p.actual_result
  const resultTag = isResolved
    ? <span className={`tag ${isCorrect ? 'tag-hit' : 'tag-miss'}`}>{isCorrect ? 'Hit' : 'Miss'}</span>
    : <span style={{ color: 'var(--text-muted)' }} aria-label={isDNP ? 'No result (DNP)' : 'No result yet'}>—</span>
  const actualDisplay = isDNP
    ? <span style={{ color: 'var(--text-muted)' }}>DNP</span>
    : isResolved
    ? <span style={{ color: sideColor(p.actual_result) }}>{p.actual_result}</span>
    : <span style={{ color: 'var(--text-muted)' }}>Pending</span>
  const predicted = <span className="font-medium" style={{ color: sideColor(p.predicted_outcome) }}>{p.predicted_outcome}</span>
  return { resultTag, actualDisplay, predicted, dateStr: formatCentral(p.timestamp),
           tableDateStr: formatCentral(p.timestamp, { suffix: false }), gameDateStr: formatGameDate(p.game_date) }
}

const LIMITED_TITLE = 'Rookie or fewer than 10 games before this game: not counted in the accuracy figures'

function LimitedBadge() {
  return (
    <span className="tag ml-1.5" title={LIMITED_TITLE}>
      limited data
    </span>
  )
}

const icon = (d) => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
       strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{d}</svg>
)
const PENCIL = icon(<path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L8 18l-4 1 1-4z" />)
const CHECK = icon(<path d="M5 12.5l4.5 4.5L19 7.5" />)
const CROSS = icon(<path d="M6 6l12 12M18 6L6 18" />)

// Set the actual result by hand: the pencil opens OVER / UNDER with confirm and cancel.
function ResultEditor({ isEditing, editValue, onEdit, onEditChange, onEditConfirm, onEditCancel, large = false }) {
  const size = large ? 'tap' : 'w-7 h-7'
  if (!isEditing) {
    return <button onClick={onEdit} className={`icon-btn ${size} flex items-center justify-center`}
                   title="Set actual result" aria-label="Set actual result">{PENCIL}</button>
  }
  return (
    <div className="flex items-center gap-1">
      <select value={editValue} onChange={(e) => onEditChange(e.target.value)} aria-label="Actual result"
              className={`input-field px-1.5 py-1 text-xs ${large ? 'tap' : ''}`}>
        <option value="OVER">OVER</option>
        <option value="UNDER">UNDER</option>
      </select>
      <button onClick={onEditConfirm} className={`btn-accent ${size} flex items-center justify-center`} aria-label="Save">{CHECK}</button>
      <button onClick={onEditCancel} className={`icon-btn ${size} flex items-center justify-center`} aria-label="Cancel">{CROSS}</button>
    </div>
  )
}

function PredictionRow({ prediction: p, editor, model }) {
  const v = rowView(p)
  return (
    <tr style={{ opacity: p.is_current === 0 ? 0.45 : 1 }}>
      <td className="px-2 py-2.5 text-xs truncate num" style={{ color: 'var(--text-muted)' }}>{v.tableDateStr}</td>
      <td className="px-2 py-2.5 text-xs truncate num" style={{ color: 'var(--text-muted)' }}>{v.gameDateStr}</td>
      <td className="px-2 py-2.5 truncate">
        <PlayerName name={p.player_name} stat={p.stat_type} league={p.league ?? 'NBA'} model={model} />
        {!!p.is_limited_data && <LimitedBadge />}
      </td>
      <td className="px-2 py-2.5 truncate" style={{ color: 'var(--text-muted)' }}>{p.stat_type}</td>
      <td className="px-2 py-2.5 truncate text-right num">{p.stat_line}</td>
      <td className="px-2 py-2.5 truncate" style={{ color: 'var(--text-muted)' }}>{p.opponent_team}</td>
      <td className="px-2 py-2.5 truncate">{v.predicted}</td>
      <td className="px-2 py-2.5 truncate text-right num">{(p.confidence * 100).toFixed(1)}%</td>
      <td className="px-2 py-2.5 truncate">{v.actualDisplay}</td>
      <td className="px-2 py-2.5">{v.resultTag}</td>
      <td className="px-1 py-1"><ResultEditor {...editor} /></td>
    </tr>
  )
}

// Phones: one two-to-four-line row per prediction instead of an 11-column table row.
function PredictionCard({ prediction: p, editor, model }) {
  const v = rowView(p)
  return (
    <li className="py-3 space-y-1" style={{ opacity: p.is_current === 0 ? 0.45 : 1 }}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 text-sm font-medium">
          <PlayerName name={p.player_name} stat={p.stat_type} league={p.league ?? 'NBA'} model={model} />
          {!!p.is_limited_data && <LimitedBadge />}
        </div>
        <span className="shrink-0 pt-3">{v.resultTag}</span>
      </div>
      <div className="text-sm num" style={{ color: 'var(--text-muted)' }}>
        {p.stat_type} {p.stat_line} · vs {p.opponent_team}
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm num">
        <span>{v.predicted} {(p.confidence * 100).toFixed(1)}%</span>
        <span style={{ color: 'var(--text-muted)' }}>Actual: {v.actualDisplay}</span>
      </div>
      <div className="flex items-center justify-between gap-3">
        <span className="text-xs num" style={{ color: 'var(--text-muted)' }}>
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
  const sorted = sortConfig.key === 'actual_graded'
    ? [...filtered].sort((a, b) => (ACTUAL_GRADED_RANK[a.actual_result] ?? 3) - (ACTUAL_GRADED_RANK[b.actual_result] ?? 3))
    : sortConfig.key
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

  const replaced = summary.all - summary.current

  return (
    <div className="history-fit w-full">
      {/* Title row: the screen's name, what the figures leave out, the one clean-up action */}
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3 mb-6">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold tracking-tight">History</h1>
          {(summary.limitedCount > 0 || replaced > 0) && (
            <p className="mt-1 text-sm max-w-3xl" style={{ color: 'var(--text-muted)' }}>
              {replaced > 0 &&
                `${replaced} replaced pick${replaced !== 1 ? 's' : ''} (dimmed: a later pick for the same player, stat and game) `}
              {summary.limitedCount > 0 &&
                `${replaced > 0 ? 'and ' : ''}${summary.limitedCount} limited-data prediction${summary.limitedCount !== 1 ? 's' : ''} `}
              not counted in these figures.
            </p>
          )}
        </div>
        <button
          onClick={askCleanDuplicates}
          disabled={cleaning || loading}
          className="btn-danger-ghost tap shrink-0 px-3 py-1.5 text-sm"
          title="Remove duplicates, keeping earliest per unique combo"
        >
          {cleaning ? 'Cleaning…' : 'Clean Duplicates'}
        </button>
      </div>

      {/* Summary: one panel split by rules; accuracy leads */}
      <div className="card summary-strip grid grid-cols-2 sm:grid-cols-5 mb-6">
        <SummaryCell label="Current picks" value={summary.total}
                     sub={`${summary.resolved} resolved${summary.all !== summary.total ? ` · ${summary.all} rows in all` : ''}`} />
        <SummaryCell label="Accuracy" value={summary.accuracy} lead
                     sub={summary.resolved > 0 ? `${summary.correct}/${summary.resolved} correct` : 'no data'} />
        {summary.tiers.map((t) => (
          <SummaryCell key={t.label} label={`Confidence ${t.label}`} value={t.value} sub={t.sub} />
        ))}
      </div>

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

      <section className="card history-table" aria-label="Predictions table">
        {/* Filters: a two-column grid on phones, one row on wider screens */}
        <div className="grid grid-cols-2 sm:flex sm:flex-wrap items-end gap-3 p-4" style={{ borderBottom: '1px solid var(--border)' }}>
          <FilterSelect value={modelFilter} onChange={setModelFilter} options={MODEL_FILTERS} label="Model" className="w-full sm:w-auto" />
          <FilterSelect value={leagueFilter} onChange={setLeagueFilter} options={LEAGUE_FILTERS} label="League" className="w-full sm:w-auto" />
          <FilterSelect value={statFilter} onChange={setStatFilter} options={ALL_STAT_TYPES} label="Stat" className="w-full sm:w-auto" />
          <FilterSelect value={outcomeFilter} onChange={setOutcomeFilter} options={OUTCOME_FILTERS} label="Outcome" className="w-full sm:w-auto" />
          <label className="flex flex-col col-span-2">
            <span className="field-label">Player</span>
            <input
              type="search"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search player…"
              aria-label="Search player"
              className="input-field h-[34px] px-2.5 text-sm w-full sm:w-48"
            />
          </label>
          {hasActiveFilter && (
            <button
              onClick={() => { setLeagueFilter('All'); setStatFilter('All'); setOutcomeFilter('All'); setSearchQuery('') }}
              className="icon-btn tap h-[34px] px-2 text-sm justify-self-start"
            >
              Clear filters
            </button>
          )}
          <div className="col-span-2 sm:ml-auto flex flex-wrap items-center justify-end gap-x-2 gap-y-3 min-h-[34px]">
            {isPhone && (
              <label className="w-full flex items-center gap-2">
                <span className="text-sm shrink-0" style={{ color: 'var(--text-muted)' }}>Sort by</span>
                <select
                  value={sortOptionValue(sortConfig)}
                  onChange={(e) => setSortConfig(sortFromOption(e.target.value))}
                  aria-label="Sort by"
                  className="input-field h-[34px] px-2 text-sm cursor-pointer min-w-0 flex-1"
                >
                  {sortSelectOptions(sortOptionValue(sortConfig)).map(([value, label]) => (
                    <option key={value} value={value}>{label}</option>
                  ))}
                </select>
              </label>
            )}
            <span className="text-sm num" style={{ color: 'var(--text-muted)' }}>{rowCountLabel(filtered)}</span>
            <span className="text-sm pl-2" style={{ color: 'var(--text-muted)' }}>Rows per page</span>
            <select
              value={pageSize}
              onChange={(e) => setPageSize(Number(e.target.value))}
              aria-label="Rows per page"
              className="input-field h-[34px] px-2 text-sm cursor-pointer num"
            >
              {PAGE_SIZES.map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </div>
        </div>

        {/* Table (desktop) or rows (phones). From 768 px the table scrolls in its own area under a sticky header;
            below that the page scrolls. */}
        {loading ? (
          <div className="px-4 py-16 text-sm" style={{ color: 'var(--text-muted)' }}>Loading…</div>
        ) : loadError ? (
          <div className="px-4 py-16 space-y-3 text-sm">
            <p>Couldn’t load the history.</p>
            <button onClick={fetchHistory} className="btn-ghost tap px-4 py-2 text-sm">Try again</button>
          </div>
        ) : filtered.length === 0 ? (
          <div className="px-4 py-16 text-sm" style={{ color: 'var(--text-muted)' }}>
            {predictions.length === 0 ? 'No predictions logged yet.' : 'No predictions match the current filters.'}
          </div>
        ) : (
          <>
            {isPhone ? (
              <ul className="px-4 phone-rows" aria-label="Predictions">
                {paginated.map((p) => (
                  <PredictionCard key={p.id} prediction={p} editor={editorFor(p)} model={TRACK_MODEL[modelFilter]} />
                ))}
              </ul>
            ) : (
              <div className="history-scroll px-2">
                <table className="data-table w-full text-sm" style={{ tableLayout: 'fixed' }}>
                  <colgroup>
                    <col style={{ width: '12%' }} />
                    <col style={{ width: '8%' }} />
                    <col style={{ width: '15%' }} />
                    <col style={{ width: '9%' }} />
                    <col style={{ width: '6%' }} />
                    <col style={{ width: '15%' }} />
                    <col style={{ width: '8%' }} />
                    <col style={{ width: '7%' }} />
                    <col style={{ width: '8%' }} />
                    <col style={{ width: '7%' }} />
                    <col style={{ width: '5%' }} />
                  </colgroup>
                  <thead>
                    <tr>
                      {SORT_COLUMNS.map(({ label, key }) => {
                        const active = sortConfig.key === key
                        const numeric = key === 'stat_line' || key === 'confidence'
                        return (
                          <th
                            key={key}
                            onClick={() => handleSort(key)}
                            aria-sort={active ? (sortConfig.direction === 'asc' ? 'ascending' : 'descending') : undefined}
                            className="sort-header px-2 py-2.5 truncate"
                            style={numeric ? { textAlign: 'right' } : undefined}
                          >
                            {label}
                            {active && (
                              <span className="ml-1" aria-hidden="true">
                                {sortConfig.direction === 'asc' ? '↑' : '↓'}
                              </span>
                            )}
                          </th>
                        )
                      })}
                      <th className="px-2 py-2.5 select-none">Result</th>
                      <th className="px-2 py-2.5"><span className="sr-only">Edit</span></th>
                    </tr>
                  </thead>
                  <tbody>
                    {paginated.map((p) => (
                      <PredictionRow
                        key={p.id}
                        prediction={p}
                        editor={editorFor(p)}
                        model={TRACK_MODEL[modelFilter]}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {/* Pagination */}
            {totalPages > 1 && (
              <div className="flex items-center justify-between gap-2 px-4 py-3" style={{ borderTop: '1px solid var(--border)' }}>
                <button
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={clampedPage === 1}
                  className="btn-ghost tap px-3 py-1.5 text-sm"
                >
                  ← Previous
                </button>
                <span className="text-sm text-center num" style={{ color: 'var(--text-muted)' }}>
                  Page {clampedPage} of {totalPages}
                </span>
                <button
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  disabled={clampedPage === totalPages}
                  className="btn-ghost tap px-3 py-1.5 text-sm"
                >
                  Next →
                </button>
              </div>
            )}
          </>
        )}
      </section>
    </div>
  )
}
