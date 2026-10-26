import { useEffect, useRef } from 'react'

// A modal question with Cancel as the default: Cancel has the focus (Enter cancels), and
// Escape or a tap outside the card also cancel. confirmLabel null shows a single OK button.
export default function ConfirmDialog({ title, message, confirmLabel, onConfirm, onCancel, danger = false }) {
  const cancelRef = useRef(null)
  useEffect(() => {
    cancelRef.current?.focus()
    const onKey = (e) => { if (e.key === 'Escape') onCancel() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onCancel])

  return (
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-4"
         style={{ backgroundColor: 'var(--scrim)', paddingBottom: 'max(1rem, env(safe-area-inset-bottom))' }}
         onMouseDown={(e) => { if (e.target === e.currentTarget) onCancel() }}>
      <div role="alertdialog" aria-modal="true" aria-labelledby="confirm-title" aria-describedby="confirm-message"
           className="floating animate-fade-in w-full max-w-sm rounded-md p-5 space-y-4">
        <h2 id="confirm-title" className="panel-title">{title}</h2>
        <p id="confirm-message" className="text-sm">{message}</p>
        <div className="flex flex-col-reverse sm:flex-row sm:justify-end gap-2">
          <button ref={cancelRef} onClick={onCancel}
                  className="btn-ghost tap px-4 py-2 text-sm font-medium">
            {confirmLabel ? 'Cancel' : 'OK'}
          </button>
          {confirmLabel && (
            <button onClick={onConfirm}
                    className="tap px-4 py-2 rounded-md text-sm font-semibold"
                    style={danger ? { backgroundColor: 'var(--negative)', color: '#fff' }
                                  : { backgroundColor: 'var(--primary)', color: 'var(--on-primary)' }}>
              {confirmLabel}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
