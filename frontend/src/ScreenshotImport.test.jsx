// Run: npx vitest run (from frontend/). The screenshot panel never resets silently: zero
// rows say "No cards found", an extractor error shows its message (e.g. GPU memory), and a
// card that couldn't be read is an unresolved row with a flag and a player search.
import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import ScreenshotImport from './ScreenshotImport'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

function upload(response) {
  globalThis.fetch = vi.fn(async () => response)
  const { container } = render(<ScreenshotImport teams={['Detroit Pistons', 'Philadelphia 76ers']} />)
  const file = new File(['png'], 'cards.png', { type: 'image/png' })
  fireEvent.change(container.querySelector('input[type="file"]'), { target: { files: [file] } })
  return container
}

it('zero rows show "No cards found"', async () => {
  upload({ ok: true, json: async () => ({ extractor: 'ollama', rows: [] }) })
  expect(await screen.findByText('No cards found')).toBeTruthy()
  expect(screen.queryByRole('table')).toBeNull()
})

it('an extractor error shows its message', async () => {
  upload({ ok: false, json: async () => ({ detail: 'Not enough GPU memory: close other apps' }) })
  expect(await screen.findByText('Not enough GPU memory: close other apps')).toBeTruthy()
})

it("a card that couldn't be read is a flagged row with a player search; the others stay", async () => {
  const ok = { player_name: 'Daniss Jenkins', resolved_player: 'Daniss Jenkins', match: 'ok', person_id: 1642450,
               stat: '3PM', line: 0.5, opponent_team: 'Philadelphia 76ers', is_home: false, is_back_to_back: false,
               game_date: '2026-10-25', flags: [], alerts: [] }
  const bad = { player_name: '', resolved_player: '', match: 'unreadable', person_id: null, stat: null, line: null,
                opponent_team: '', is_home: false, is_back_to_back: false, game_date: null, flags: [],
                alerts: ["couldn't read this card"], unreadable: true }
  upload({ ok: true, json: async () => ({ extractor: 'ollama', rows: [ok, bad] }) })
  expect(await screen.findByText('Predict 1 row')).toBeTruthy()
  expect(screen.getAllByRole('row')).toHaveLength(3)                         // header + 2 cards
  expect(screen.getByTitle("couldn't read this card")).toBeTruthy()
  expect(screen.getAllByRole('combobox', { hidden: true }).some((el) => el.tagName === 'INPUT')).toBe(true)
})
