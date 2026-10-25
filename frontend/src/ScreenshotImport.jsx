import { useState } from 'react'
import { SCREENSHOT_STATS, toEditable, isReady, predictBody, selectPlayer, typePlayer } from './screenshotRows'
import PlayerSearch from './components/PlayerSearch'

const API = 'http://localhost:8000'

// Screenshot -> extracted rows (text only) -> editable confirmation table -> Predict,
// which calls the normal /predict once per ready row. Nothing is predicted before that.
export default function ScreenshotImport({ teams }) {
  const [rows, setRows] = useState([])
  const [extracting, setExtracting] = useState(false)
  const [predicting, setPredicting] = useState(false)
  const [error, setError] = useState('')
  const [results, setResults] = useState({})

  const extract = async (file) => {
    if (!file || !file.type.startsWith('image/')) return
    setExtracting(true)
    setError('')
    setRows([])
    setResults({})
    try {
      const form = new FormData()
      form.append('file', file)
      const res = await fetch(`${API}/screenshot/extract`, { method: 'POST', body: form })
      const data = await res.json()
      if (!res.ok) throw new Error(data.detail || 'Could not read the screenshot')
      setRows(data.rows.map(toEditable))
    } catch (e) {
      setError(e.message || 'Could not read the screenshot')
    } finally {
      setExtracting(false)
    }
  }

  const onPaste = (e) => {
    const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith('image/'))
    if (item) {
      e.preventDefault()
      extract(item.getAsFile())
    }
  }

  const update = (key, field, value) =>
    setRows((rs) => rs.map((r) => (r.key === key ? { ...r, [field]: value } : r)))
  const remove = (key) => setRows((rs) => rs.filter((r) => r.key !== key))
  const edit = (key, fn) => setRows((rs) => rs.map((r) => (r.key === key ? fn(r) : r)))

  // An unresolved row's player picked in its search: the same team lookup the manual form makes
  const pick = async (key, name, suggestion) => {
    edit(key, (r) => selectPlayer(r, name, suggestion, null))
    try {
      const res = await fetch(`${API}/players/${encodeURIComponent(name)}/team`)
      const team = res.ok ? await res.json() : null
      edit(key, (r) => (r.player_name === name ? selectPlayer(r, name, suggestion, team) : r))
    } catch {}
  }

  const predictAll = async () => {
    setPredicting(true)
    const out = {}
    for (const r of rows.filter(isReady)) {
      try {
        const res = await fetch(`${API}/predict`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(predictBody(r)),
        })
        const data = await res.json()
        out[r.key] = res.ok ? { outcome: data.outcome, confidence: data.confidence }
                            : { error: data.detail || 'failed' }
      } catch {
        out[r.key] = { error: 'failed' }
      }
      setResults({ ...out })
    }
    setPredicting(false)
  }

  const ready = rows.filter(isReady).length
  // Same field style as the History filters and the Predict form, sized for a table cell
  const cell = 'input-field w-full rounded-xl px-3 py-2 text-sm'
  const th = 'px-2 py-2 font-semibold'

  return (
    <div className="card w-full max-w-4xl rounded-2xl p-6 shadow-2xl space-y-4 mb-6">
      <div tabIndex={0} onPaste={onPaste} className="flex items-center justify-between gap-3 outline-none">
        <span className="text-sm font-semibold" style={{ color: 'var(--text-secondary)' }}>
          {extracting ? 'Reading screenshot…' : 'Screenshot import (paste or choose)'}
        </span>
        <label className={`btn-ghost px-4 py-2 rounded-xl text-sm font-medium ${extracting ? 'opacity-30 cursor-not-allowed' : 'cursor-pointer'}`}>
          Choose image
          <input type="file" accept="image/*" className="hidden" disabled={extracting}
                 onChange={(e) => { extract(e.target.files?.[0]); e.target.value = '' }} />
        </label>
      </div>

      {error && <div className="animate-fade-in status-out border rounded-xl px-4 py-3 text-sm">{error}</div>}

      {rows.length > 0 && (
        <div className="animate-fade-in space-y-4">
          <div className="rounded-2xl border overflow-x-auto" style={{ borderColor: 'var(--border)' }}>
            <table className="w-full text-sm" style={{ tableLayout: 'fixed', minWidth: '640px' }}>
              <colgroup>
                <col style={{ width: '26%' }} />
                <col style={{ width: '16%' }} />
                <col style={{ width: '10%' }} />
                <col style={{ width: '25%' }} />
                <col style={{ width: '7%' }} />
                <col style={{ width: '12%' }} />
                <col style={{ width: '4%' }} />
              </colgroup>
              <thead>
                <tr className="text-left text-xs uppercase tracking-widest border-b"
                    style={{ borderBottomColor: 'var(--border)', color: 'var(--text-muted)' }}>
                  <th className={th}>Player</th>
                  <th className={th}>Stat</th>
                  <th className={th}>Line</th>
                  <th className={th}>Opponent</th>
                  <th className={`${th} text-center`}>Home</th>
                  <th className={th}>Result</th>
                  <th className={th} />
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => {
                  const res = results[r.key]
                  return (
                    <tr key={r.key} className="align-top"
                        style={{ backgroundColor: i % 2 ? 'var(--bg-stripe)' : 'transparent' }}>
                      <td className="px-2 py-2">
                        <div className="relative">
                          {r.searchable ? (
                            <PlayerSearch value={r.player_name} placeholder={r.card_name} label={null} compact
                                          onChange={(q) => edit(r.key, (x) => typePlayer(x, q))}
                                          onSelect={(name, s) => pick(r.key, name, s)}
                                          onClear={() => edit(r.key, (x) => typePlayer(x, ''))} />
                          ) : (
                            <input className={`${cell} ${r.alerts.length ? 'pr-8' : ''}`} value={r.player_name}
                                   placeholder={r.card_name}
                                   onChange={(e) => edit(r.key, (x) => ({ ...x, player_name: e.target.value, person_id: null }))} />
                          )}
                          {r.alerts.length > 0 && (
                            <span className={`absolute ${r.searchable ? 'right-9' : 'right-3'} top-1/2 -translate-y-1/2 cursor-help`}
                                  title={r.alerts.join('\n')} style={{ color: 'var(--warning)' }}>⚑</span>
                          )}
                        </div>
                        {r.flags.map((f) => (
                          <div key={f} className="mt-1 text-xs" style={{ color: 'var(--warning)' }}>{f}</div>
                        ))}
                      </td>
                      <td className="px-2 py-2">
                        <select className={`${cell} cursor-pointer`} value={r.stat_type}
                                onChange={(e) => update(r.key, 'stat_type', e.target.value)}>
                          <option value="" />
                          {SCREENSHOT_STATS.map((s) => <option key={s} value={s}>{s}</option>)}
                        </select>
                      </td>
                      <td className="px-2 py-2">
                        <input className={cell} inputMode="decimal" value={r.stat_line}
                               onChange={(e) => update(r.key, 'stat_line', e.target.value)} />
                        {(r.higher != null || r.lower != null) && (
                          <div className="mt-1 text-xs" style={{ color: 'var(--text-muted)' }}>
                            {r.higher != null ? `H ${r.higher}x` : ''} {r.lower != null ? `L ${r.lower}x` : ''}
                          </div>
                        )}
                      </td>
                      <td className="px-2 py-2">
                        <select className={`${cell} cursor-pointer`} value={r.opponent_team}
                                onChange={(e) => update(r.key, 'opponent_team', e.target.value)}>
                          <option value="" />
                          {teams.map((t) => <option key={t} value={t}>{t}</option>)}
                        </select>
                      </td>
                      <td className="px-2 py-2 text-center align-middle">
                        <input type="checkbox" checked={r.is_home}
                               onChange={(e) => update(r.key, 'is_home', e.target.checked)} />
                      </td>
                      <td className="px-2 py-2 align-middle truncate">
                        {res?.error && <span className="text-xs" style={{ color: 'var(--danger)' }}>{res.error}</span>}
                        {res?.outcome && (
                          <span className="font-semibold"
                                style={{ color: res.outcome === 'OVER' ? 'var(--success)' : 'var(--danger)' }}>
                            {res.outcome} {res.confidence}%
                          </span>
                        )}
                      </td>
                      <td className="px-1 py-2 text-center align-middle">
                        <button className="icon-btn text-lg leading-none" onClick={() => remove(r.key)}
                                aria-label="Remove row">×</button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          <button
            onClick={predictAll}
            disabled={predicting || ready === 0}
            className="btn-accent w-full py-3.5 rounded-xl font-bold text-sm uppercase tracking-widest"
          >
            {predicting ? 'Predicting…' : `Predict ${ready} row${ready !== 1 ? 's' : ''}`}
          </button>
        </div>
      )}
    </div>
  )
}
