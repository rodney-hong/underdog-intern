// Where a dropdown menu goes, in viewport (position: fixed) coordinates, so no scrolling or
// rounded container can clip it: below the input, or above it when there isn't room below
// and there is more room above.
export const MENU_MAX_HEIGHT = 240
const GAP = 4
const MIN_HEIGHT = 80

export function menuPlacement(rect, viewportHeight, menuHeight = MENU_MAX_HEIGHT) {
  const want = Math.min(menuHeight, MENU_MAX_HEIGHT)
  const below = viewportHeight - rect.bottom - GAP
  const above = rect.top - GAP
  const up = below < want && above > below
  const room = up ? above : below
  return {
    up,
    left: rect.left,
    width: rect.width,
    maxHeight: Math.max(MIN_HEIGHT, Math.min(MENU_MAX_HEIGHT, room)),
    ...(up ? { bottom: viewportHeight - rect.top + GAP } : { top: rect.bottom + GAP }),
  }
}
