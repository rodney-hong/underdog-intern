import { useState, useEffect, useCallback } from 'react'

const API = 'http://localhost:8000'
const PAGE_SIZES = [10, 25, 50]

const ALL_STAT_TYPES = [
  'All', 'Points', 'Assists', 'Rebounds', 'PRA', '3PM', 'PR', 'PA', 'RA',
  'Blocks', 'Steals', 'Blocks+Steals', 'Turnovers',
  'Offensive Rebounds', 'Defensive Rebounds', 'Double Double', '3PA',
]
const OUTCOME_FILTERS = ['All', 'Correct', 'Incorrect', 'Pending']
const LEAGUE_FILTERS = ['All', 'NBA', 'NFL', 'WNBA']

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

function FilterSelect({ value, onChange, options }) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="input-field rounded-xl px-3 py-2 text-sm cursor-pointer"
    >
      {options.map((o) => (
        <option key={o} value={o}>{o}</option>
      ))}
    </select>
  )
}

// ---------------------------------------------------------------------------
// Prediction row
// ---------------------------------------------------------------------------

function PredictionRow({ prediction: p, isEditing, editValue, onEdit, onEditChange, onEditConfirm, onEditCancel, stripe }) {
  const isOver = p.predicted_outcome === 'OVER'
  const isDNP = p.actual_result === 'DNP'
  const isResolved = p.actual_result !== null && !isDNP
  const isCorrect = isResolved && p.predicted_outcome === p.actual_result

  const resultIcon = isDNP
    ? <span style={{ color: 'var(--text-muted)' }}>—</span>
    : isResolved ? (isCorrect ? '✅' : '❌') : '⏳'

  const actualDisplay = isDNP
    ? <span style={{ color: 'var(--text-muted)' }}>DNP</span>
    : isResolved
    ? <span className="font-semibold" style={{ color: p.actual_result === 'OVER' ? 'var(--success)' : 'var(--danger)' }}>{p.actual_result}</span>
    : <span style={{ color: 'var(--text-muted)' }}>Pending</span>

  const dateStr = (() => {
    try {
      const d = new Date(p.timestamp)
      return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) +
        ' ' + d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
    } catch {
      return p.timestamp
    }
  })()

  const gameDateStr = p.game_date
    ? (() => {
        const [y, m, d] = p.game_date.split('-').map(Number)
        return new Date(y, m - 1, d).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
      })()
    : '—'

  return (
    <tr style={{ backgroundColor: stripe ? 'var(--bg-stripe)' : 'transparent' }}>
      <td className="px-2 py-2 text-xs truncate" style={{ color: 'var(--text-muted)' }}>{dateStr}</td>
      <td className="px-2 py-2 text-xs truncate" style={{ color: 'var(--text-muted)' }}>{gameDateStr}</td>
      <td className="px-2 py-2 truncate" style={{ color: 'var(--text-secondary)' }}>{p.player_name}</td>
      <td className="px-2 py-2 truncate" style={{ color: 'var(--text-muted)' }}>{p.stat_type}</td>
      <td className="px-2 py-2 truncate" style={{ color: 'var(--text-secondary)' }}>{p.stat_line}</td>
      <td className="px-2 py-2 truncate" style={{ color: 'var(--text-muted)' }}>{p.opponent_team}</td>
      <td className="px-2 py-2 truncate">
        <span className="font-semibold" style={{ color: isOver ? 'var(--success)' : 'var(--danger)' }}>
          {p.predicted_outcome}
        </span>
      </td>
      <td className="px-2 py-2 truncate" style={{ color: 'var(--text-secondary)' }}>{(p.confidence * 100).toFixed(1)}%</td>
      <td className="px-2 py-2 truncate">{actualDisplay}</td>
      <td className="px-2 py-2 text-center">{resultIcon}</td>
      <td className="px-2 py-2">
        {isEditing ? (
          <div className="flex items-center gap-1.5">
            <select
              value={editValue}
              onChange={(e) => onEditChange(e.target.value)}
              className="input-field rounded-lg px-2 py-1 text-xs"
            >
              <option value="OVER">OVER</option>
              <option value="UNDER">UNDER</option>
            </select>
            <button onClick={onEditConfirm} className="btn-accent text-xs px-2 py-1 rounded-lg">✓</button>
            <button onClick={onEditCancel} className="icon-btn text-xs px-1 py-1">✕</button>
          </div>
        ) : (
          <button onClick={onEdit} className="icon-btn text-sm px-1" title="Set actual result">✏</button>
        )}
      </td>
    </tr>
  )
}

// ---------------------------------------------------------------------------
// Main History component
// ---------------------------------------------------------------------------

export default function History() {
  const [predictions, setPredictions] = useState([])
  const [loading, setLoading] = useState(true)
  const [cleaning, setCleaning] = useState(false)
  const [statFilter, setStatFilter] = useState('All')
  const [outcomeFilter, setOutcomeFilter] = useState('All')
  const [leagueFilter, setLeagueFilter] = useState('All')
  const [searchQuery, setSearchQuery] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  const [editingId, setEditingId] = useState(null)
  const [editValue, setEditValue] = useState('OVER')
  const [sortConfig, setSortConfig] = useState({ key: null, direction: 'asc' })

  const fetchHistory = useCallback(async () => {
    setLoading(true)
    try {
      const res = await fetch(`${API}/predictions/history`)
      const data = await res.json()
      setPredictions(data)
    } catch {
      setPredictions([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchHistory() }, [fetchHistory])

  // Reset to page 1 whenever filters, search, page size, or sort change
  useEffect(() => { setPage(1) }, [statFilter, outcomeFilter, leagueFilter, searchQuery, pageSize, sortConfig])

  const handleOverride = async (id) => {
    try {
      await fetch(`${API}/predictions/${id}/result`, {
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

  const handleCleanDuplicates = async () => {
    setCleaning(true)
    try {
      await fetch(`${API}/predictions/duplicates`, { method: 'DELETE' })
      fetchHistory()
    } catch {}
    finally { setCleaning(false) }
  }

  // Derived summary stats (always over full dataset, excluding DNP)
  const resolved = predictions.filter((p) => p.actual_result !== null && p.actual_result !== 'DNP')
  const correct = resolved.filter((p) => p.predicted_outcome === p.actual_result)
  const overallAcc = resolved.length > 0
    ? `${(correct.length / resolved.length * 100).toFixed(1)}%`
    : '—'

  const tierStats = [
    { label: '50–60%', min: 0.5, max: 0.6 },
    { label: '60–70%', min: 0.6, max: 0.7 },
    { label: '70%+',   min: 0.7, max: 1.01 },
  ].map((tier) => {
    const t = resolved.filter((p) => p.confidence >= tier.min && p.confidence < tier.max)
    const c = t.filter((p) => p.predicted_outcome === p.actual_result)
    return {
      label: tier.label,
      value: t.length ? `${(c.length / t.length * 100).toFixed(0)}%` : '—',
      sub: `${t.length} game${t.length !== 1 ? 's' : ''}`,
    }
  })

  // Filter pipeline
  const filtered = predictions.filter((p) => {
    if (leagueFilter !== 'All' && (p.league ?? 'NBA') !== leagueFilter) return false
    if (statFilter !== 'All' && p.stat_type !== statFilter) return false
    if (outcomeFilter === 'Pending') return p.actual_result === null || p.actual_result === 'DNP'
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

  return (
    <div className="w-full max-w-6xl mx-auto px-4">
      {/* Header row: summary cards + clean button */}
      <div className="flex items-start gap-4 mb-6">
        <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 flex-1">
          <SummaryCard label="Total" value={predictions.length} sub={`${resolved.length} resolved`} />
          <SummaryCard label="Accuracy" value={overallAcc} sub={resolved.length > 0 ? `${correct.length}/${resolved.length} correct` : 'no data'} />
          {tierStats.map((t) => (
            <SummaryCard key={t.label} label={t.label} value={t.value} sub={t.sub} />
          ))}
        </div>
        <div className="shrink-0 pt-1">
          <button
            onClick={handleCleanDuplicates}
            disabled={cleaning}
            className="btn-danger-ghost px-3 py-2 rounded-xl text-xs"
            title="Remove duplicates, keeping earliest per unique combo"
          >
            {cleaning ? 'Cleaning…' : 'Clean Duplicates'}
          </button>
        </div>
      </div>

      {/* Filters row */}
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <span className="text-xs uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>Filter:</span>
        <FilterSelect value={leagueFilter} onChange={setLeagueFilter} options={LEAGUE_FILTERS} />
        <FilterSelect value={statFilter} onChange={setStatFilter} options={ALL_STAT_TYPES} />
        <FilterSelect value={outcomeFilter} onChange={setOutcomeFilter} options={OUTCOME_FILTERS} />
        <input
          type="text"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="Search player…"
          className="input-field rounded-xl px-3 py-2 text-sm w-40"
        />
        {hasActiveFilter && (
          <button
            onClick={() => { setLeagueFilter('All'); setStatFilter('All'); setOutcomeFilter('All'); setSearchQuery('') }}
            className="icon-btn text-xs"
          >
            Clear
          </button>
        )}
        <div className="ml-auto flex items-center gap-2">
          <span className="text-xs" style={{ color: 'var(--text-muted)' }}>Rows per page:</span>
          <select
            value={pageSize}
            onChange={(e) => setPageSize(Number(e.target.value))}
            className="input-field rounded-xl px-2 py-2 text-sm cursor-pointer"
          >
            {PAGE_SIZES.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          <span className="text-xs" style={{ color: 'var(--text-muted)' }}>{filtered.length} row{filtered.length !== 1 ? 's' : ''}</span>
        </div>
      </div>

      {/* Table */}
      {loading ? (
        <div className="text-center py-16" style={{ color: 'var(--text-muted)' }}>Loading…</div>
      ) : filtered.length === 0 ? (
        <div className="text-center py-16" style={{ color: 'var(--text-muted)' }}>
          {predictions.length === 0 ? 'No predictions logged yet.' : 'No predictions match the current filters.'}
        </div>
      ) : (
        <>
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
                  {[
                    { label: 'Date',      key: 'timestamp' },
                    { label: 'Game Date', key: 'game_date' },
                    { label: 'Player',    key: 'player_name' },
                    { label: 'Stat',      key: 'stat_type' },
                    { label: 'Line',      key: 'stat_line' },
                    { label: 'Opponent',  key: 'opponent_team' },
                    { label: 'Predicted', key: 'predicted_outcome' },
                    { label: 'Conf.',     key: 'confidence' },
                    { label: 'Actual',    key: 'actual_result' },
                  ].map(({ label, key }) => {
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
                    isEditing={editingId === p.id}
                    editValue={editValue}
                    onEdit={() => { setEditingId(p.id); setEditValue('OVER') }}
                    onEditChange={setEditValue}
                    onEditConfirm={() => handleOverride(p.id)}
                    onEditCancel={() => setEditingId(null)}
                    stripe={i % 2 === 1}
                  />
                ))}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between mt-4 px-1">
              <button
                onClick={() => setPage((p) => Math.max(1, p - 1))}
                disabled={clampedPage === 1}
                className="btn-ghost px-4 py-2 rounded-xl text-sm"
              >
                ← Previous
              </button>
              <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
                Page {clampedPage} of {totalPages}
              </span>
              <button
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                disabled={clampedPage === totalPages}
                className="btn-ghost px-4 py-2 rounded-xl text-sm"
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
