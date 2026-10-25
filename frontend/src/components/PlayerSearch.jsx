import { useState, useEffect, useRef, useCallback } from 'react'
import { ClearButton, Label } from './fields'

const API = 'http://localhost:8000'

// Player search with an autocomplete dropdown: the manual Predict form and unresolved
// screenshot rows. NBA search returns {name, person_id, team}; WNBA returns plain names.
export default function PlayerSearch({ value, onChange, onSelect, onClear, searchUrl, placeholder,
                                      label = 'Player Search', compact = false }) {
  const [suggestions, setSuggestions] = useState([])
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)
  const debounceRef = useRef(null)
  const containerRef = useRef(null)

  const fetchSuggestions = useCallback(async (q) => {
    if (!q.trim()) { setSuggestions([]); setOpen(false); return }
    setLoading(true)
    try {
      const url = searchUrl || `${API}/players/search`
      const res = await fetch(`${url}?q=${encodeURIComponent(q)}`)
      const data = await res.json()
      setSuggestions(data)
      setOpen(data.length > 0)
    } catch {
      setSuggestions([])
    } finally {
      setLoading(false)
    }
  }, [searchUrl])

  const handleChange = (e) => {
    const q = e.target.value
    onChange(q)
    clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => fetchSuggestions(q), 300)
  }

  // onSelect(name, suggestion): NBA suggestions carry person_id and team
  const handleSelect = (name, suggestion) => {
    setOpen(false)
    setSuggestions([])
    onSelect(name, suggestion)
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
      {label && <Label>{label}</Label>}
      <div className="relative">
        <input
          type="text"
          value={value}
          onChange={handleChange}
          onFocus={() => suggestions.length > 0 && setOpen(true)}
          placeholder={placeholder ?? 'Search players…'}
          className={`input-field w-full rounded-xl pr-10 ${compact ? 'px-3 py-2 text-sm' : 'px-4 py-3'}`}
        />
        {loading && (
          <span className="absolute right-3 top-1/2 -translate-y-1/2 text-sm" style={{ color: 'var(--text-muted)' }}>…</span>
        )}
        {!loading && <ClearButton visible={!!value} onClick={handleClear} />}
      </div>
      {open && (
        <ul className="card absolute z-50 mt-1 w-full rounded-xl shadow-2xl overflow-hidden animate-slide-down">
          {suggestions.map((s) => {
            // NBA search returns {name, person_id, team}; WNBA search returns plain names.
            const name = typeof s === 'string' ? s : s.name
            return (
              <li
                key={typeof s === 'string' ? s : s.person_id}
                onMouseDown={() => handleSelect(name, typeof s === 'string' ? null : s)}
                className="suggestion-item px-4 py-2.5 cursor-pointer text-sm"
              >
                {name}
                {typeof s !== 'string' && s.team && (
                  <span className="ml-2 text-xs" style={{ color: 'var(--text-muted)' }}>{s.team}</span>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
