import { useState } from 'react'
import { SCREENSHOT_STATS, toEditable, isReady, predictBody, selectPlayer, typePlayer, unresolvedNote,
         opponentPlaceholder } from './screenshotRows'
import PlayerSearch from './components/PlayerSearch'
import { apiFetch } from './api'
import { useIsPhone } from './useMediaQuery'
import { sideColor } from './sides'

// The server refuses uploads over 10 MB (screenshot_import.MAX_IMAGE_BYTES) and scales every
// image to 2,000 px on its longer side; a bigger phone image is scaled to that here first, in
// memory, so it uploads quickly and is never refused.
const MAX_UPLOAD_BYTES = 9.5 * 1024 * 1024
const MAX_SIDE = 2000

async function shrinkIfNeeded(file) {
  if (file.size <= MAX_UPLOAD_BYTES || typeof createImageBitmap !== 'function') return file
  const bitmap = await createImageBitmap(file)
  const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height))
  const canvas = document.createElement('canvas')
  canvas.width = Math.round(bitmap.width * scale)
  canvas.height = Math.round(bitmap.height * scale)
  canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height)
  bitmap.close?.()
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'))
  return blob && blob.size <= MAX_UPLOAD_BYTES ? new File([blob], 'screenshot.png', { type: 'image/png' }) : file
}

// The ⚑ on an uncertain row: a button (tap or click) that shows why, not a hover-only tooltip.
function AlertFlag({ alerts, open, onToggle, className = '' }) {
  if (!alerts.length) return null
  return (
    <button type="button" onClick={onToggle} title={alerts.join('\n')} aria-expanded={open}
            aria-label="Why this row is flagged"
            className={`icon-btn tap flex items-center justify-center ${className}`} style={{ color: 'var(--text)' }}>
      ⚑{/* taste-skill-ignore: the flag glyph is a kept feature */}
    </button>
  )
}

function AlertList({ alerts }) {
  return (
    <ul className="mt-1 space-y-0.5 text-xs font-medium">
      {alerts.map((a) => <li key={a}>⚑ {a}</li>)}{/* taste-skill-ignore */}
    </ul>
  )
}

function ResultText({ res }) {
  if (!res) return null
  if (res.error) return <span className="text-xs" style={{ color: 'var(--text-muted)' }}>{res.error}</span>
  return (
    <span className="num" style={{ color: sideColor(res.outcome) }}>
      <span className="font-semibold">{res.outcome}</span> {res.confidence}%
    </span>
  )
}

// Screenshot(s) -> extracted rows (text only) -> editable confirmation table -> Predict,
// which calls the normal /predict once per ready row. Nothing is predicted before that.
export default function ScreenshotImport({ teams }) {
  const [rows, setRows] = useState([])
  const [extracting, setExtracting] = useState('')     // progress text while reading
  const [predicting, setPredicting] = useState(false)
  const [errors, setErrors] = useState([])
  const [results, setResults] = useState({})
  const [openAlerts, setOpenAlerts] = useState({})
  const isPhone = useIsPhone()

  // One or more images, read one after another; their rows go into one confirmation list.
  const extractAll = async (files) => {
    const images = [...(files || [])].filter((f) => f && f.type.startsWith('image/'))
    if (!images.length) return
    setErrors([])
    setRows([])
    setResults({})
    setOpenAlerts({})
    let nextKey = 0
    const found = []
    const errs = []
    for (const [i, file] of images.entries()) {
      const which = images.length > 1 ? `Screenshot ${i + 1} of ${images.length}: ` : ''
      setExtracting(images.length > 1 ? `Reading screenshot ${i + 1} of ${images.length}…` : 'Reading screenshot…')
      try {
        const form = new FormData()
        form.append('file', await shrinkIfNeeded(file))
        const res = await apiFetch('/screenshot/extract', { method: 'POST', body: form })
        const data = await res.json()
        if (!res.ok) throw new Error(data.detail || 'Could not read the screenshot')
        if (!data.rows?.length) throw new Error('No cards found')
        found.push(...data.rows.map((r) => toEditable(r, nextKey++)))
        setRows([...found])
      } catch (e) {
        errs.push(which + (e.message || 'Could not read the screenshot'))
        setErrors([...errs])
      }
    }
    setExtracting('')
  }

  const onPaste = (e) => {
    const files = [...(e.clipboardData?.items || [])].filter((i) => i.type.startsWith('image/')).map((i) => i.getAsFile())
    if (files.length) {
      e.preventDefault()
      extractAll(files)
    }
  }

  const update = (key, field, value) =>
    setRows((rs) => rs.map((r) => (r.key === key ? { ...r, [field]: value } : r)))
  const remove = (key) => setRows((rs) => rs.filter((r) => r.key !== key))
  const edit = (key, fn) => setRows((rs) => rs.map((r) => (r.key === key ? fn(r) : r)))
  const toggleAlerts = (key) => setOpenAlerts((o) => ({ ...o, [key]: !o[key] }))

  // An unresolved row's player picked in its search: the same team lookup the manual form makes
  const pick = async (key, name, suggestion) => {
    edit(key, (r) => selectPlayer(r, name, suggestion, null))
    // Remember the card's name for this player (the server skips collision and roster names)
    const card = rows.find((r) => r.key === key)?.card_name
    if (card && suggestion?.person_id) {
      apiFetch('/screenshot/alias', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ screenshot_name: card, person_id: suggestion.person_id }),
      }).catch(() => {})
    }
    try {
      const res = await apiFetch(`/players/${encodeURIComponent(name)}/team`)
      const team = res.ok ? await res.json() : null
      edit(key, (r) => (r.player_name === name ? selectPlayer(r, name, suggestion, team) : r))
    } catch {}
  }

  const predictAll = async () => {
    setPredicting(true)
    const out = {}
    for (const r of rows.filter(isReady)) {
      try {
        const res = await apiFetch('/predict', {
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
  const cell = 'input-field w-full px-2.5 py-1.5 text-sm'
  const th = 'px-2 py-2'

  const playerField = (r) => (r.searchable ? (
    <PlayerSearch value={r.player_name} placeholder={r.card_name} label={null} compact
                  onChange={(q) => edit(r.key, (x) => typePlayer(x, q))}
                  onSelect={(name, s) => pick(r.key, name, s)}
                  onClear={() => edit(r.key, (x) => typePlayer(x, ''))} />
  ) : (
    <input className={cell} value={r.player_name} placeholder={r.card_name} aria-label="Player"
           onChange={(e) => edit(r.key, (x) => ({ ...x, player_name: e.target.value, person_id: null }))} />
  ))

  const statField = (r) => (
    <select className={`${cell} cursor-pointer`} value={r.stat_type} aria-label="Stat"
            onChange={(e) => update(r.key, 'stat_type', e.target.value)}>
      <option value="" />
      {SCREENSHOT_STATS.map((s) => <option key={s} value={s}>{s}</option>)}
    </select>
  )

  const lineField = (r) => (
    <input className={`${cell} num sm:text-right`} inputMode="decimal" value={r.stat_line} aria-label="Line"
           onChange={(e) => update(r.key, 'stat_line', e.target.value)} />
  )

  const multipliers = (r) => (r.higher != null || r.lower != null) && (
    <div className="mt-1 text-xs num" style={{ color: 'var(--text-muted)' }}>
      {r.higher != null ? `H ${r.higher}x` : ''} {r.lower != null ? `L ${r.lower}x` : ''}
    </div>
  )

  const opponentField = (r) => (
    <select className={`${cell} cursor-pointer`} value={r.opponent_team} aria-label="Opponent"
            style={r.opponent_team ? undefined : { color: 'var(--text-muted)' }}
            onChange={(e) => update(r.key, 'opponent_team', e.target.value)}>
      <option value="">{opponentPlaceholder(r)}</option>
      {teams.map((t) => <option key={t} value={t}>{t}</option>)}
    </select>
  )

  // Under the player: what an unresolved row needs (until a player is picked), then flags
  const flags = (r) => {
    const note = unresolvedNote(r)
    return (
      <>
        {note && <div className="mt-1 text-xs font-medium">⚑ {note}</div>}{/* taste-skill-ignore */}
        {r.flags.map((f) => (
          <div key={f} className="mt-1 text-xs font-medium">{f}</div>
        ))}
      </>
    )
  }

  return (
    <section className="card w-full p-4 sm:p-5 space-y-4" aria-labelledby="screenshot-import">
      <div tabIndex={0} onPaste={onPaste} className="flex flex-wrap items-center justify-between gap-3 outline-none">
        <div className="min-w-0">
          <h2 id="screenshot-import" className="panel-title">Screenshot import</h2>
          <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
            {extracting || (isPhone ? 'Underdog screenshots' : 'Paste or choose Underdog screenshots')}
          </p>
        </div>
        <label className={`btn-ghost tap inline-flex items-center justify-center px-4 py-2 text-sm font-medium ${
          extracting ? 'opacity-30 cursor-not-allowed' : 'cursor-pointer'} ${isPhone ? 'w-full' : ''}`}>
          {isPhone ? 'Choose screenshots' : 'Choose images'}
          <input type="file" accept="image/*" multiple className="hidden" disabled={!!extracting}
                 onChange={(e) => { extractAll(e.target.files); e.target.value = '' }} />
        </label>
      </div>

      {errors.map((err) => (
        <div key={err} role="alert" className="notice animate-fade-in px-3 py-2 text-sm">{err}</div>
      ))}

      {rows.length > 0 && (
        <div className="animate-fade-in space-y-4">
          {isPhone ? (
            <ul aria-label="Rows to confirm">
              {rows.map((r, i) => (
                <li key={r.key} className="py-3 space-y-3" style={i ? { borderTop: '1px solid var(--border)' } : undefined}>
                  <div className="flex items-start gap-1">
                    <div className="flex-1 min-w-0">
                      {playerField(r)}
                      {flags(r)}
                      {openAlerts[r.key] && <AlertList alerts={r.alerts} />}
                    </div>
                    <AlertFlag alerts={r.alerts} open={!!openAlerts[r.key]} onToggle={() => toggleAlerts(r.key)} />
                    <button className="icon-btn tap flex items-center justify-center text-2xl leading-none"
                            onClick={() => remove(r.key)} aria-label="Remove row">×</button>
                  </div>
                  <div className="grid grid-cols-2 gap-2">
                    <div>{statField(r)}</div>
                    <div>{lineField(r)}{multipliers(r)}</div>
                  </div>
                  {opponentField(r)}
                  <div className="flex items-center justify-between gap-3">
                    <label className="tap inline-flex items-center gap-3 text-sm cursor-pointer">
                      <input type="checkbox" className="h-5 w-5" checked={r.is_home}
                             onChange={(e) => update(r.key, 'is_home', e.target.checked)} />
                      Home game
                    </label>
                    <ResultText res={results[r.key]} />
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <div className="overflow-x-auto -mx-1">
              <table className="data-table w-full text-sm" style={{ tableLayout: 'fixed', minWidth: '640px' }}>
                <colgroup>
                  <col style={{ width: '25%' }} />
                  <col style={{ width: '15%' }} />
                  <col style={{ width: '9%' }} />
                  <col style={{ width: '24%' }} />
                  <col style={{ width: '6%' }} />
                  <col style={{ width: '17%' }} />
                  <col style={{ width: '4%' }} />
                </colgroup>
                <thead>
                  <tr>
                    <th className={th}>Player</th>
                    <th className={th}>Stat</th>
                    <th className={`${th} text-right`} style={{ textAlign: 'right' }}>Line</th>
                    <th className={th}>Opponent</th>
                    <th className={th} style={{ textAlign: 'center' }}>Home</th>
                    <th className={th}>Result</th>
                    <th className={th} />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.key} className="align-top">
                      <td className="px-2 py-2">
                        <div className="relative">
                          <div className={r.alerts.length ? 'pr-6' : ''}>{playerField(r)}</div>
                          <AlertFlag alerts={r.alerts} open={!!openAlerts[r.key]} onToggle={() => toggleAlerts(r.key)}
                                     className="absolute right-0 top-1/2 -translate-y-1/2 h-8 w-6" />
                        </div>
                        {flags(r)}
                        {openAlerts[r.key] && <AlertList alerts={r.alerts} />}
                      </td>
                      <td className="px-2 py-2">{statField(r)}</td>
                      <td className="px-2 py-2 text-right">{lineField(r)}{multipliers(r)}</td>
                      <td className="px-2 py-2">{opponentField(r)}</td>
                      <td className="px-2 py-2 text-center align-middle">
                        <input type="checkbox" checked={r.is_home} aria-label="Home"
                               onChange={(e) => update(r.key, 'is_home', e.target.checked)} />
                      </td>
                      <td className="px-2 py-2 align-middle truncate"><ResultText res={results[r.key]} /></td>
                      <td className="px-1 py-2 text-center align-middle">
                        <button className="icon-btn px-1 text-lg leading-none" onClick={() => remove(r.key)}
                                aria-label="Remove row">×</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className={isPhone ? 'sticky-action' : ''}>
            <button
              onClick={predictAll}
              disabled={predicting || ready === 0}
              className="btn-accent tap w-full py-2.5 text-sm"
            >
              {predicting ? 'Predicting…' : `Predict ${ready} row${ready !== 1 ? 's' : ''}`}
            </button>
          </div>
        </div>
      )}
    </section>
  )
}
