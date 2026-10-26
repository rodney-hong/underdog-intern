// Small form pieces shared by the Predict form, the player search and the screenshot table.

export function Label({ children }) {
  return (
    <label className="field-label">
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
      className="icon-btn absolute right-1 w-11 h-11 flex items-center justify-center sm:right-1 sm:w-8 sm:h-8 top-1/2 -translate-y-1/2 text-lg leading-none"
      aria-label="Clear"
    >
      ×
    </button>
  )
}
