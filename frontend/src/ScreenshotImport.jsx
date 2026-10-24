import { useState } from 'react'
import { SCREENSHOT_STATS, toEditable, isReady, predictBody, loggedLineDiffers } from './screenshotRows'

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
  const cell = 'input-field rounded-lg px-2 py-1 text-xs w-full'

  return (
    <div className="w-full max-w-3xl mb-6 space-y-3">
      <div
        tabIndex={0}
        onPaste={onPaste}
        className="card rounded-2xl p-4 flex items-center justify-between gap-3 outline-none"
      >
        <span className="text-xs font-semibold uppercase tracking-widest" style={{ color: 'var(--text-muted)' }}>
          {extracting ? 'Reading screenshot…' : 'Screenshot import (paste or choose)'}
        </span>
        <label className="btn-ghost px-3 py-1.5 rounded-lg text-xs font-medium cursor-pointer">
          Choose image
          <input type="file" accept="image/*" className="hidden" disabled={extracting}
                 onChange={(e) => { extract(e.target.files?.[0]); e.target.value = '' }} />
        </label>
      </div>

      {error && <div className="status-out border rounded-xl px-4 py-2 text-xs">{error}</div>}

      {rows.length > 0 && (
        <div className="card rounded-2xl p-3 overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr style={{ color: 'var(--text-muted)' }}>
                <th className="text-left p-1">Player</th>
                <th className="text-left p-1">Stat</th>
                <th className="text-left p-1">Line</th>
                <th className="text-left p-1">Opponent</th>
                <th className="p-1">Home</th>
                <th className="text-left p-1">Result</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const res = results[r.key]
                const logged = loggedLineDiffers(r)
                return (
                  <tr key={r.key} className="align-top">
                    <td className="p-1 min-w-[160px]">
                      <div className="flex items-center gap-1">
                        {r.alerts.length > 0 && (
                          <span title={r.alerts.join('\n')} style={{ color: 'var(--warning)' }}>⚑</span>
                        )}
                        <input className={cell} value={r.player_name} placeholder={r.card_name}
                               onChange={(e) => update(r.key, 'player_name', e.target.value)} />
                      </div>
                      {r.flags.map((f) => (
                        <div key={f} className="mt-0.5" style={{ color: 'var(--warning)' }}>{f}</div>
                      ))}
                    </td>
                    <td className="p-1">
                      <select className={cell} value={r.stat_type}
                              onChange={(e) => update(r.key, 'stat_type', e.target.value)}>
                        <option value="" />
                        {SCREENSHOT_STATS.map((s) => <option key={s} value={s}>{s}</option>)}
                      </select>
                    </td>
                    <td className="p-1 w-24">
                      <div className="flex items-center gap-1">
                        <input className={cell} inputMode="decimal" value={r.stat_line}
                               onChange={(e) => update(r.key, 'stat_line', e.target.value)} />
                        {logged != null && (
                          <span style={{ color: 'var(--text-muted)' }}
                                title={`${r.logged_line.pull_phase} ${r.logged_line.captured_at}`}>{logged}</span>
                        )}
                      </div>
                      {(r.higher != null || r.lower != null) && (
                        <div className="mt-0.5" style={{ color: 'var(--text-muted)' }}>
                          {r.higher != null ? `H ${r.higher}x` : ''} {r.lower != null ? `L ${r.lower}x` : ''}
                        </div>
                      )}
                    </td>
                    <td className="p-1 min-w-[150px]">
                      <select className={cell} value={r.opponent_team}
                              onChange={(e) => update(r.key, 'opponent_team', e.target.value)}>
                        <option value="" />
                        {teams.map((t) => <option key={t} value={t}>{t}</option>)}
                      </select>
                    </td>
                    <td className="p-1 text-center">
                      <input type="checkbox" checked={r.is_home}
                             onChange={(e) => update(r.key, 'is_home', e.target.checked)} />
                    </td>
                    <td className="p-1 whitespace-nowrap">
                      {res?.error && <span style={{ color: 'var(--danger)' }}>{res.error}</span>}
                      {res?.outcome && (
                        <span className="font-semibold"
                              style={{ color: res.outcome === 'OVER' ? 'var(--success)' : 'var(--danger)' }}>
                          {res.outcome} {res.confidence}%
                        </span>
                      )}
                    </td>
                    <td className="p-1">
                      <button className="btn-ghost rounded px-1.5" onClick={() => remove(r.key)}
                              aria-label="Remove row">×</button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          <button
            onClick={predictAll}
            disabled={predicting || ready === 0}
            className="btn-accent w-full mt-3 py-2.5 rounded-xl font-bold text-xs uppercase tracking-widest"
          >
            {predicting ? 'Predicting…' : `Predict ${ready} row${ready !== 1 ? 's' : ''}`}
          </button>
        </div>
      )}
    </div>
  )
}
