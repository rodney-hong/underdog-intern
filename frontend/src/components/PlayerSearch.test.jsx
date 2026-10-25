// Run: npx vitest run (from frontend/). The screenshot table's last-row player search shows
// its options outside the table's scroll container (portal), opens upward near the bottom of
// the viewport, and works with the keyboard like the manual Predict form's search.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import ScreenshotImport from '../ScreenshotImport'
import PlayerSearch from './PlayerSearch'
import { menuPlacement } from './menuPlacement'

const row = (over) => ({
  player_name: 'X', resolved_player: '', stat: 'Points', line: 10.5, opponent_team: '', is_home: false,
  is_back_to_back: false, game_date: null, match: 'not_found', person_id: null, flags: [], alerts: [], ...over,
})
const EXTRACTED = [
  row({ player_name: 'LeBron James', resolved_player: 'LeBron James', match: 'ok', person_id: 2544,
        opponent_team: 'Utah Jazz', game_date: '2026-10-25' }),
  row({ player_name: 'Austin Reaves', resolved_player: 'Austin Reaves', match: 'ok', person_id: 1630559,
        opponent_team: 'Utah Jazz', game_date: '2026-10-25' }),
  row({ player_name: 'Jaxson Hayes', alerts: ['player not resolved: no exact roster match'] }),   // last row
]
const HAYES = { name: 'Jaxson Hayes', person_id: 1629637, team: 'Utah Jazz' }
const TEAM = { player_team: { full: 'Utah Jazz' }, next_opponent: { full: 'Los Angeles Lakers' }, is_home: true,
               is_back_to_back: false, next_game_date: '2026-10-25' }

function mockFetch() {
  return vi.fn(async (url) => {
    const body = url.includes('/screenshot/extract') ? { rows: EXTRACTED }
      : url.includes('/players/search') ? [HAYES]
      : url.includes('/team') ? TEAM : {}
    return { ok: true, json: async () => body }
  })
}

beforeEach(() => {
  globalThis.fetch = mockFetch()
  window.innerHeight = 800
  // jsdom has no layout: put every combobox input at the bottom of the viewport (a last row)
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function () {
    return this.getAttribute?.('role') === 'combobox'
      ? { top: 750, bottom: 790, left: 40, right: 340, width: 300, height: 40 }
      : { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }
  })
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

async function loadTable() {
  const { container } = render(<ScreenshotImport teams={['Los Angeles Lakers', 'Utah Jazz']} />)
  const file = new File(['png'], 'cards.png', { type: 'image/png' })
  fireEvent.change(container.querySelector('input[type="file"]'), { target: { files: [file] } })
  await screen.findByText('Predict 2 rows')
  return container
}

describe('screenshot table, last row, unresolved player', () => {
  it('shows the search options outside the scroll container, opening upward', async () => {
    const container = await loadTable()
    const search = screen.getByPlaceholderText('Jaxson Hayes')  // the unresolved row's player search
    fireEvent.change(search, { target: { value: 'Jaxson Hayes' } })
    const option = await screen.findByRole('option', { name: /Jaxson Hayes/ })
    expect(option).toBeTruthy()
    expect(globalThis.fetch).toHaveBeenCalledWith(expect.stringContaining('/players/search?q=Jaxson%20Hayes'))
    const list = screen.getByRole('listbox')
    const scroller = container.querySelector('.overflow-x-auto')
    expect(scroller.contains(list)).toBe(false)                 // not clipped by the table container
    expect(list.style.position === 'fixed' || list.className.includes('fixed')).toBe(true)
    expect(list.style.bottom).not.toBe('')                      // no room below: opens upward
    expect(list.style.top).toBe('')
    expect(search.getAttribute('aria-expanded')).toBe('true')
  })

  it('picks with the keyboard and the row joins Predict with the team context', async () => {
    await loadTable()
    const search = screen.getByPlaceholderText('Jaxson Hayes')
    fireEvent.change(search, { target: { value: 'Jaxson' } })
    await screen.findByRole('option', { name: /Jaxson Hayes/ })
    fireEvent.keyDown(search, { key: 'ArrowDown' })
    expect(screen.getByRole('option', { name: /Jaxson Hayes/ }).getAttribute('aria-selected')).toBe('true')
    fireEvent.keyDown(search, { key: 'Enter' })
    await screen.findByText('Predict 3 rows')
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(globalThis.fetch).toHaveBeenCalledWith(expect.stringContaining('/players/Jaxson%20Hayes/team'))
    const table = screen.getByRole('table')
    expect(within(table).getByDisplayValue('Los Angeles Lakers')).toBeTruthy()   // opponent filled
  })

  it('Escape closes the options', async () => {
    await loadTable()
    const search = screen.getByPlaceholderText('Jaxson Hayes')
    fireEvent.change(search, { target: { value: 'Jaxson' } })
    await screen.findByRole('listbox')
    fireEvent.keyDown(search, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull())
  })
})

describe('the same component in the manual Predict form', () => {
  it('ArrowUp wraps, Enter with nothing highlighted picks the first option', async () => {
    const picked = vi.fn()
    globalThis.fetch = vi.fn(async () => ({ ok: true, json: async () => [HAYES, { name: 'Jaxson Hayes Sr.', person_id: 9, team: 'X' }] }))
    function Form() {
      return <PlayerSearch value="Jax" onChange={() => {}} onSelect={picked} onClear={() => {}} />
    }
    render(<Form />)
    const search = screen.getByRole('combobox')
    await act(async () => { fireEvent.change(search, { target: { value: 'Jaxs' } }) })
    await screen.findAllByRole('option')
    fireEvent.keyDown(search, { key: 'ArrowUp' })
    expect(screen.getAllByRole('option')[1].getAttribute('aria-selected')).toBe('true')
    fireEvent.keyDown(search, { key: 'Escape' })                 // closes and clears the highlight
    expect(screen.queryByRole('listbox')).toBeNull()
    fireEvent.focus(search)                                       // reopens with the same options
    fireEvent.keyDown(search, { key: 'Enter' })
    expect(picked).toHaveBeenCalledWith('Jaxson Hayes', expect.objectContaining({ person_id: 1629637 }))
  })
})

describe('menuPlacement', () => {
  it('opens below with room, above near the bottom', () => {
    const r = (top) => ({ top, bottom: top + 40, left: 10, width: 200 })
    expect(menuPlacement(r(100), 800)).toMatchObject({ up: false, top: 144 })
    expect(menuPlacement(r(750), 800)).toMatchObject({ up: true, bottom: 54, maxHeight: 240 })
  })
})
