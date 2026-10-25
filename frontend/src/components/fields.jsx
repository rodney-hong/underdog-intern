// Small form pieces shared by the Predict form, the player search and the screenshot table.

export function Label({ children }) {
  return (
    <label className="block text-xs font-semibold uppercase tracking-widest mb-1.5" style={{ color: 'var(--text-muted)' }}>
      {children}
    </label>
  )
}

export function ClearButton({ onClick, visible }) {
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
