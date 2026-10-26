// Run: npx vitest run (from frontend/). The player tooltip's track record follows the model
// filter: Challenger -> model=challenger, Frozen -> frozen, Challenger retro ->
// challenger_retro, All -> all.
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import History, { rowCountLabel } from './History'

const pred = (id, player, model) => ({
  id, timestamp: `2026-10-2${id}T12:00:00`, player_name: player, stat_type: 'Points', stat_line: 20.5,
  opponent_team: 'Boston Celtics', predicted_outcome: 'OVER', confidence: 0.6, explanation: 'e',
  actual_result: 'OVER', game_date: '2026-10-21', league: 'NBA', is_limited_data: 0, model, is_current: 1,
})
const HISTORY = [pred(1, 'Cee Player', 'challenger'), pred(2, 'Eff Player', 'frozen'), pred(3, 'Ar Player', 'challenger_retro')]
const TR = { wins: 1, losses: 0, n: 1, raw_rate: 1, adjusted_rate: 0.6, overall_rate: 0.55, enough: false,
             raw_rate_ci95: [0.2, 1] }

beforeEach(() => {
  globalThis.fetch = vi.fn(async (url) => ({ ok: true, json: async () => (url.includes('/predictions/history') ? HISTORY : TR) }))
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

const trackCalls = () => globalThis.fetch.mock.calls.map(([u]) => u).filter((u) => u.includes('/track-record'))

it('the tooltip asks for the model the filter shows', async () => {
  render(<History />)
  fireEvent.mouseEnter(await screen.findByText('Cee Player'))           // default filter: Challenger
  expect(trackCalls().at(-1)).toContain('model=challenger')
  expect(trackCalls().at(-1)).not.toContain('challenger_retro')

  const filter = screen.getByDisplayValue('Challenger')
  for (const [label, player, model] of [['Frozen', 'Eff Player', 'model=frozen'],
                                         ['Challenger retro', 'Ar Player', 'model=challenger_retro'],
                                         ['All', 'Cee Player', 'model=all']]) {
    fireEvent.change(filter, { target: { value: label } })
    fireEvent.mouseEnter(await screen.findByText(player))
    expect(trackCalls().at(-1)).toContain(model)
    expect(trackCalls().at(-1)).toContain('stat=Points')
  }
})

it('Clean Duplicates asks first, says how many, and Cancel deletes nothing', async () => {
  const dupes = [...HISTORY, { ...HISTORY[0], id: 9 }, { ...HISTORY[0], id: 10 }]   // 2 extra copies of one pick
  globalThis.fetch = vi.fn(async (url) => ({ ok: true, status: 200,
    json: async () => (url.includes('/predictions/history') ? dupes : url.includes('/duplicates') ? { deleted: 2 } : TR) }))
  render(<History />)
  await screen.findAllByText('Cee Player')
  const deletes = () => globalThis.fetch.mock.calls.filter(([, o]) => o?.method === 'DELETE')

  fireEvent.click(screen.getByRole('button', { name: 'Clean Duplicates' }))
  const dialog = screen.getByRole('alertdialog')
  expect(dialog.textContent).toContain('permanently removes 2 duplicate predictions')
  expect(document.activeElement.textContent).toBe('Cancel')              // Cancel is the default
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(screen.queryByRole('alertdialog')).toBeNull()
  expect(deletes()).toHaveLength(0)

  fireEvent.click(screen.getByRole('button', { name: 'Clean Duplicates' }))
  fireEvent.keyDown(document, { key: 'Escape' })                           // Escape cancels too
  expect(deletes()).toHaveLength(0)

  fireEvent.click(screen.getByRole('button', { name: 'Clean Duplicates' }))
  fireEvent.click(screen.getByRole('button', { name: 'Remove 2' }))
  expect(deletes()).toHaveLength(1)
})

it('with nothing to remove it says so and offers only OK', async () => {
  render(<History />)
  await screen.findAllByText('Cee Player')
  fireEvent.click(screen.getByRole('button', { name: 'Clean Duplicates' }))
  expect(screen.getByRole('alertdialog').textContent).toContain('no duplicate predictions')
  expect(screen.queryByRole('button', { name: /^Remove/ })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'OK' }))
  expect(globalThis.fetch.mock.calls.some(([, o]) => o?.method === 'DELETE')).toBe(false)
})

it('the row count separates current picks from replaced ones', () => {
  expect(rowCountLabel([{ is_current: 1 }, { is_current: 1 }])).toBe('2 rows')
  expect(rowCountLabel([{ is_current: 1 }, { is_current: 0 }, { is_current: 1 }])).toBe('2 current · 3 total')
})
