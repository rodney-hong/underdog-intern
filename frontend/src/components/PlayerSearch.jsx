import { useState, useEffect, useLayoutEffect, useRef, useCallback, useId } from 'react'
import { createPortal } from 'react-dom'
import { ClearButton, Label } from './fields'
import { menuPlacement, visibleArea } from './menuPlacement'
import { apiFetch } from '../api'

// Player search with an autocomplete dropdown: the manual Predict form and unresolved
// screenshot rows. NBA search returns {name, person_id, team}; WNBA returns plain names.
// The menu is rendered in a portal on <body> at page coordinates (menuPlacement), so a
// scrolling or rounded container (the screenshot table) never clips it; it opens upward
// when there isn't room below in the visible area (short screens, the phone keyboard).
// Keys: ArrowDown / ArrowUp move, Enter picks (the first option when none is highlighted),
// Escape closes.
export default function PlayerSearch({ value, onChange, onSelect, onClear, searchPath = '/players/search', placeholder,
                                      label = 'Player Search', compact = false }) {
  const [suggestions, setSuggestions] = useState([])
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)
  const [active, setActive] = useState(-1)
  const [place, setPlace] = useState(null)
  const debounceRef = useRef(null)
  const containerRef = useRef(null)
  const inputRef = useRef(null)
  const menuRef = useRef(null)
  const listId = useId()

  const fetchSuggestions = useCallback(async (q) => {
    if (!q.trim()) { setSuggestions([]); setOpen(false); return }
    setLoading(true)
    try {
      const res = await apiFetch(`${searchPath}?q=${encodeURIComponent(q)}`)
      const data = res.ok === false ? [] : await res.json()
      setSuggestions(data)
      setActive(-1)
      setOpen(data.length > 0)
    } catch {
      setSuggestions([])
    } finally {
      setLoading(false)
    }
  }, [searchPath])

  const handleChange = (e) => {
    const q = e.target.value
    onChange(q)
    clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => fetchSuggestions(q), 300)
  }

  const nameOf = (s) => (typeof s === 'string' ? s : s.name)

  // onSelect(name, suggestion): NBA suggestions carry person_id and team
  const handleSelect = (s) => {
    setOpen(false)
    setSuggestions([])
    setActive(-1)
    onSelect(nameOf(s), typeof s === 'string' ? null : s)
  }

  const handleClear = () => {
    setSuggestions([])
    setOpen(false)
    onClear()
  }

  const handleKeyDown = (e) => {
    const n = suggestions.length
    if (e.key === 'ArrowDown' && n) {
      e.preventDefault()
      setOpen(true)
      setActive((a) => (a + 1) % n)
    } else if (e.key === 'ArrowUp' && n) {
      e.preventDefault()
      setOpen(true)
      setActive((a) => (a <= 0 ? n - 1 : a - 1))
    } else if (e.key === 'Enter' && open && n) {
      e.preventDefault()
      handleSelect(suggestions[active >= 0 ? active : 0])
    } else if (e.key === 'Escape' && open) {
      e.preventDefault()
      setOpen(false)
      setActive(-1)
    }
  }

  // Position the portal menu against the input; follow scrolling (any ancestor), resizes, and
  // the visual viewport (the phone keyboard opening or closing).
  // The list's height comes from the data (options x row height), never from the menu as
  // rendered, so placing it can't feed back into itself. The row height is measured from an
  // option once the menu exists, else the CSS row (44 px on phones, 40 px otherwise).
  const update = useCallback(() => {
    if (!inputRef.current) return
    const phone = window.matchMedia?.('(max-width: 639px)').matches
    const row = menuRef.current?.querySelector('[role=option]')?.offsetHeight || (phone ? 44 : 40)
    const next = menuPlacement(inputRef.current.getBoundingClientRect(), visibleArea(),
                               { x: window.scrollX, y: window.scrollY }, suggestionCount.current * row, row)
    setPlace((prev) => (prev && ['up', 'left', 'width', 'height', 'top'].every((k) => prev[k] === next[k]) ? prev : next))
  }, [])
  const suggestionCount = useRef(0)
  suggestionCount.current = suggestions.length

  useLayoutEffect(() => {
    if (!open) return undefined
    update()
    const vv = window.visualViewport
    window.addEventListener('scroll', update, true)
    window.addEventListener('resize', update)
    vv?.addEventListener('resize', update)
    vv?.addEventListener('scroll', update)
    return () => {
      window.removeEventListener('scroll', update, true)
      window.removeEventListener('resize', update)
      vv?.removeEventListener('resize', update)
      vv?.removeEventListener('scroll', update)
    }
  }, [open, suggestions, update])

  useEffect(() => {
    const handler = (e) => {
      if (containerRef.current?.contains(e.target) || menuRef.current?.contains(e.target)) return
      setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  useEffect(() => () => clearTimeout(debounceRef.current), [])

  const menu = open && place && createPortal(
    <ul
      ref={menuRef}
      id={listId}
      role="listbox"
      data-placement={place.up ? 'up' : 'down'}
      className="card z-50 rounded-xl shadow-2xl overflow-y-auto"
      style={{ position: 'absolute', left: place.left, top: place.top, width: place.width,
               height: place.height, overscrollBehavior: 'contain' }}
    >
      {suggestions.map((s, i) => (
        <li
          key={typeof s === 'string' ? s : s.person_id}
          id={`${listId}-${i}`}
          role="option"
          aria-selected={i === active}
          onMouseDown={(e) => { e.preventDefault(); handleSelect(s) }}
          onMouseEnter={() => setActive(i)}
          className="suggestion-item tap-row px-4 py-2.5 cursor-pointer text-sm"
          style={i === active ? { backgroundColor: 'var(--bg-tertiary)' } : undefined}
        >
          {nameOf(s)}
          {typeof s !== 'string' && s.team && (
            <span className="ml-2 text-xs" style={{ color: 'var(--text-muted)' }}>{s.team}</span>
          )}
        </li>
      ))}
    </ul>,
    document.body,
  )

  return (
    <div ref={containerRef} className="relative">
      {label && <Label>{label}</Label>}
      <div className="relative">
        <input
          ref={inputRef}
          type="text"
          role="combobox"
          aria-expanded={!!open}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={open && active >= 0 ? `${listId}-${active}` : undefined}
          value={value}
          onChange={handleChange}
          onKeyDown={handleKeyDown}
          onFocus={() => suggestions.length > 0 && setOpen(true)}
          placeholder={placeholder ?? 'Search players…'}
          className={`input-field w-full rounded-xl pr-10 ${compact ? 'px-3 py-2 text-sm' : 'px-4 py-3'}`}
        />
        {loading && (
          <span className="absolute right-3 top-1/2 -translate-y-1/2 text-sm" style={{ color: 'var(--text-muted)' }}>…</span>
        )}
        {!loading && <ClearButton visible={!!value} onClick={handleClear} />}
      </div>
      {menu}
    </div>
  )
}
