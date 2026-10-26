// Where a dropdown menu goes. It is rendered in a portal on <body> at page coordinates
// (position: absolute), so no scrolling or rounded container clips it, it moves with the page,
// and a full-page screenshot shows it where the screen does. It opens below the input, or above
// it when there isn't room below and there is more room above. "Room" is measured in the
// visual viewport: on a phone the on-screen keyboard covers the bottom of the layout viewport
// (iOS keeps window.innerHeight unchanged), so a menu opened "below" there would be hidden.
// The height is a whole number of rows, so no option is ever cut in half.
export const MENU_MAX_HEIGHT = 240
const GAP = 4
const MIN_ROWS = 2
const BORDERS = 2      // the menu's 1 px top and bottom border sit inside its height (border-box)

// rect: the input's getBoundingClientRect(); view: { top, height } of the visible area in client
// coordinates (window.visualViewport, else 0 / innerHeight); scroll: { x, y } page offset;
// contentHeight: the options' total height; rowHeight: one option's height (0 when unknown).
// The returned height includes the menu's borders.
export function menuPlacement(rect, view, scroll = { x: 0, y: 0 }, contentHeight = MENU_MAX_HEIGHT, rowHeight = 0) {
  const full = contentHeight + BORDERS
  const below = view.top + view.height - rect.bottom - GAP
  const above = rect.top - view.top - GAP
  const want = Math.min(full, MENU_MAX_HEIGHT)
  const up = below < want && above > below
  const room = Math.max(up ? above : below, 0)
  let height = Math.min(full, MENU_MAX_HEIGHT, room)
  if (rowHeight > 0 && height < full) {
    // scrollable: show whole rows only (at least MIN_ROWS, even on a very short screen)
    height = Math.max(MIN_ROWS, Math.floor((height - BORDERS) / rowHeight)) * rowHeight + BORDERS
  }
  height = Math.max(height, Math.min(full, (rowHeight || 80) + BORDERS))
  return {
    up,
    left: rect.left + scroll.x,
    width: rect.width,
    height,
    top: up ? rect.top + scroll.y - GAP - height : rect.bottom + scroll.y + GAP,
  }
}

// The visible area right now (visual viewport where the browser has one).
export function visibleArea() {
  const vv = typeof window !== 'undefined' ? window.visualViewport : null
  return vv ? { top: vv.offsetTop, height: vv.height } : { top: 0, height: window.innerHeight }
}
