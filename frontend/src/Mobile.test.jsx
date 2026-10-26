// Run: npx vitest run (from frontend/). Phone behaviour: History shows stacked cards (no
// table) with timestamps in Central time; the track-record tooltip opens on tap; several
// screenshots can be chosen at once and their rows land in one list of cards, with a failed
// image named; the ⚑ on a flagged row opens its reasons on tap.
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import History from './History'
import ScreenshotImport from './ScreenshotImport'

const pred = {
  id: 1, timestamp: '2026-10-25T19:39:00.253072', player_name: 'Cee Player', stat_type: 'Points', stat_line: 20.5,
  opponent_team: 'Boston Celtics', predicted_outcome: 'OVER', confidence: 0.6, explanation: 'e',
  actual_result: null, game_date: '2026-10-25', league: 'NBA', is_limited_data: 0, model: 'challenger', is_current: 1,
}
const TR = { wins: 3, losses: 1, n: 4, raw_rate: 0.75, adjusted_rate: 0.6, overall_rate: 0.55, enough: false,
             raw_rate_ci95: [0.3, 0.95] }

function phone(matches) {
  window.matchMedia = vi.fn((q) => ({ matches, media: q, addEventListener: () => {}, removeEventListener: () => {} }))
}

beforeEach(() => phone(true))
afterEach(() => { cleanup(); vi.restoreAllMocks(); delete window.matchMedia })

it('History on a phone: cards, Central time, tap opens the tooltip', async () => {
  globalThis.fetch = vi.fn(async (url) => ({ ok: true, status: 200,
    json: async () => (url.includes('/predictions/history') ? [pred] : TR) }))
  render(<History />)
  const cards = await screen.findByRole('list', { name: 'Predictions' })
  expect(screen.queryByRole('table')).toBeNull()
  expect(within(cards).getByText(/made Oct 25, 2:39 PM CT/)).toBeTruthy()   // 19:39 UTC
  fireEvent.click(within(cards).getByRole('button', { name: 'Cee Player' }))
  expect(await screen.findByText('Record: 3–1 (4 picks)')).toBeTruthy()
})

it('a tap keeps the tooltip open through the emulated mouse events that follow it', async () => {
  globalThis.fetch = vi.fn(async (url) => ({ ok: true, status: 200,
    json: async () => (url.includes('/predictions/history') ? [pred] : TR) }))
  render(<History />)
  const name = await screen.findByRole('button', { name: 'Cee Player' })
  fireEvent.pointerDown(name, { pointerType: 'touch' })
  fireEvent.mouseEnter(name)                      // emulated by the browser after a touch
  fireEvent.click(name)
  fireEvent.mouseLeave(name)                      // emulated mouse lands elsewhere after a scroll
  expect(await screen.findByRole('tooltip')).toBeTruthy()
  fireEvent.pointerDown(name, { pointerType: 'touch' })
  await new Promise((r) => setTimeout(r, 450))
  fireEvent.click(name)                           // a second tap closes it
  expect(screen.queryByRole('tooltip')).toBeNull()
})

it('History on desktop keeps the table, dates in Central time', async () => {
  phone(false)
  globalThis.fetch = vi.fn(async (url) => ({ ok: true, status: 200,
    json: async () => (url.includes('/predictions/history') ? [pred] : TR) }))
  render(<History />)
  const table = await screen.findByRole('table')
  expect(within(table).getByText('Date (CT)')).toBeTruthy()
  expect(within(table).getByText('Oct 25, 2:39 PM')).toBeTruthy()
})

const row = (name, extra = {}) => ({
  player_name: name, resolved_player: name, match: 'ok', person_id: 1, stat: 'Points', line: 20.5,
  opponent_team: 'Utah Jazz', is_home: false, is_back_to_back: false, game_date: '2026-10-25', flags: [], alerts: [], ...extra,
})

it('several screenshots at once: rows from each, a failed image is named', async () => {
  const replies = [
    { ok: true, status: 200, json: async () => ({ rows: [row('Ann One'), row('Bob Two', { alerts: ['two reads disagree on line'] })] }) },
    { ok: false, status: 503, json: async () => ({ detail: 'Screenshot reading is unavailable right now' }) },
    { ok: true, status: 200, json: async () => ({ rows: [row('Cal Three')] }) },
  ]
  globalThis.fetch = vi.fn(async () => replies.shift())
  const { container } = render(<ScreenshotImport teams={['Utah Jazz', 'Boston Celtics']} />)
  const input = container.querySelector('input[type="file"]')
  expect(input.multiple).toBe(true)
  expect(input.accept).toBe('image/*')
  const files = ['a', 'b', 'c'].map((n) => new File(['png'], `${n}.png`, { type: 'image/png' }))
  fireEvent.change(input, { target: { files } })

  expect(await screen.findByText('Predict 3 rows')).toBeTruthy()
  expect(screen.getByRole('alert').textContent).toBe('Screenshot 2 of 3: Screenshot reading is unavailable right now')
  const list = screen.getByRole('list', { name: 'Rows to confirm' })
  expect(within(list).getAllByRole('listitem')).toHaveLength(3)
  expect(screen.queryByRole('table')).toBeNull()

  // the flag opens its reasons on tap
  fireEvent.click(screen.getByRole('button', { name: 'Why this row is flagged' }))
  expect(screen.getByText('⚑ two reads disagree on line')).toBeTruthy()
  // the Predict button sits in the sticky bar
  expect(screen.getByText('Predict 3 rows').parentElement.className).toContain('sticky-action')
})

it('History on a phone: "Sort by" offers Newest, Confidence and Actual over the whole filtered set', async () => {
  const rows = [['Zed Last', 0.55, null], ['Amy First', 0.72, 'DNP'], ['Mo Middle', 0.61, 'UNDER'], ['Bo Fourth', 0.58, 'OVER']]
    .map(([player_name, confidence, actual_result], i) => ({ ...pred, id: i + 1, player_name, confidence, actual_result }))
  globalThis.fetch = vi.fn(async (url) => ({ ok: true, status: 200,
    json: async () => (url.includes('/predictions/history') ? rows : TR) }))
  render(<History />)
  const cards = await screen.findByRole('list', { name: 'Predictions' })
  const names = () => within(cards).getAllByRole('button', { name: /Last|First|Middle|Fourth/ }).map((b) => b.textContent)
  expect(names()).toEqual(['Zed Last', 'Amy First', 'Mo Middle', 'Bo Fourth'])     // Newest: the server's order

  const sortBy = screen.getByRole('combobox', { name: 'Sort by' })
  expect([...sortBy.options].map((o) => o.textContent)).toEqual(['Newest', 'Confidence', 'Actual'])
  fireEvent.change(sortBy, { target: { value: 'confidence:desc' } })                // high to low
  expect(names()).toEqual(['Amy First', 'Mo Middle', 'Bo Fourth', 'Zed Last'])
  fireEvent.change(sortBy, { target: { value: 'actual_graded:asc' } })              // OVER, UNDER, DNP, Pending
  expect(names()).toEqual(['Bo Fourth', 'Mo Middle', 'Amy First', 'Zed Last'])
  fireEvent.change(sortBy, { target: { value: '' } })
  expect(names()).toEqual(['Zed Last', 'Amy First', 'Mo Middle', 'Bo Fourth'])
})

it('"Sort by" shows only with the phone list; the sort carries across the switch to the table', async () => {
  // A matchMedia that can change, so the list / table switch happens as on a resize
  let matches = true
  const listeners = new Set()
  window.matchMedia = vi.fn((q) => ({ get matches() { return matches }, media: q,
    addEventListener: (_, fn) => listeners.add(fn), removeEventListener: (_, fn) => listeners.delete(fn) }))
  const resize = (phoneNow) => act(() => { matches = phoneNow; listeners.forEach((fn) => fn()) })
  globalThis.fetch = vi.fn(async (url) => ({ ok: true, status: 200,
    json: async () => (url.includes('/predictions/history') ? [pred] : TR) }))
  render(<History />)
  await screen.findByRole('list', { name: 'Predictions' })
  fireEvent.change(screen.getByRole('combobox', { name: 'Sort by' }), { target: { value: 'confidence:desc' } })

  resize(false)                                                      // to the table
  const table = await screen.findByRole('table')
  expect(screen.queryByRole('combobox', { name: 'Sort by' })).toBeNull()
  expect(within(table).getByText('Conf.').closest('th').getAttribute('aria-sort')).toBe('descending')
  fireEvent.click(within(table).getByText('Player'))                // a sort the phone doesn't offer

  resize(true)                                                       // back to the list
  await screen.findByRole('list', { name: 'Predictions' })
  const sortBy = screen.getByRole('combobox', { name: 'Sort by' })
  expect(sortBy.value).toBe('player_name:asc')                       // shown under its own label, not mislabelled
  expect(sortBy.selectedOptions[0].textContent).toBe('Player, A to Z')
  fireEvent.change(sortBy, { target: { value: '' } })                // picking another drops the extra option
  expect([...sortBy.options].map((o) => o.textContent)).toEqual(['Newest', 'Confidence', 'Actual'])
})
