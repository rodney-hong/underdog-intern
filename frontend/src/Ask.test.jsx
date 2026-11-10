// Run: npx vitest run (from frontend/). Ask lives in History: a side panel on wide screens, a
// bottom sheet otherwise. The explain button on a row sends that pick's prediction id; questions
// post the conversation to /assistant and show the answer with the tools it came from.
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import History from './History'

const pick = (id, player, stat, line, side) => ({
  id, timestamp: '2026-10-29T18:26:00', player_name: player, stat_type: stat, stat_line: line,
  opponent_team: 'Portland Trail Blazers', predicted_outcome: side, confidence: 0.606, explanation: 'e',
  actual_result: null, game_date: '2026-10-29', league: 'NBA', is_limited_data: 0, model: 'challenger', is_current: 1,
})
const HISTORY = [pick(5718, 'Ja Morant', '3PM', 1.5, 'UNDER'), pick(5719, 'Donovan Clingan', '3PM', 0.5, 'OVER')]
const json = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body })

let reply
function screenWidth({ phone = false, wide = false }) {
  window.matchMedia = vi.fn((q) => ({
    matches: q.includes('max-width: 639px') ? phone : q.includes('min-width: 1280px') ? wide : false,
    media: q, addEventListener: () => {}, removeEventListener: () => {},
  }))
}

beforeEach(() => {
  reply = json(200, { answer: 'ok', tools_used: [] })
  globalThis.fetch = vi.fn(async (url) => {
    if (url.includes('/predictions/history')) return json(200, HISTORY)
    if (url.includes('/assistant')) return reply
    return json(200, {})
  })
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); delete window.matchMedia })

const assistantCalls = () => globalThis.fetch.mock.calls.filter(([u]) => u.includes('/assistant'))
const lastBody = () => JSON.parse(assistantCalls().at(-1)[1].body)

it('the Ask button opens the panel; a question shows the answer with its sources', async () => {
  screenWidth({ wide: true })
  reply = json(200, {
    answer: 'Ja Morant 3PM 1.5 UNDER, 61%: 2–0 (2 picks), adjusted 63%, small sample.',
    tools_used: [{ name: 'list_pending', arguments: {} }, { name: 'list_pending', arguments: { stat: '3PM' } }],
  })
  render(<History />)
  await screen.findByText('Ja Morant')
  expect(screen.queryByRole('complementary', { name: 'Ask' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
  const panel = screen.getByRole('complementary', { name: 'Ask' })
  expect(panel.className).toContain('ask-side')                       // beside the table on wide screens

  fireEvent.change(within(panel).getByLabelText('Question'), { target: { value: 'best picks?' } })
  fireEvent.click(within(panel).getByRole('button', { name: 'Send' }))
  expect(await within(panel).findByText(/2–0 \(2 picks\), adjusted 63%/)).toBeTruthy()
  expect(within(panel).getByText('From: pending picks')).toBeTruthy()
  expect(assistantCalls()[0][0]).toBe('/api/assistant')
  expect(lastBody()).toEqual({ messages: [{ role: 'user', content: 'best picks?' }] })   // no prediction_id

  fireEvent.click(within(panel).getByRole('button', { name: 'Close Ask' }))
  expect(screen.queryByRole('complementary', { name: 'Ask' })).toBeNull()
})

it('the explain button on a table row sends that pick’s prediction id', async () => {
  screenWidth({ wide: true })
  render(<History />)
  const row = (await screen.findByText('Ja Morant')).closest('tr')
  fireEvent.click(within(row).getByRole('button', { name: 'Explain this pick' }))

  expect(screen.getByRole('complementary', { name: 'Ask' })).toBeTruthy()  // opens the panel
  await vi.waitFor(() => expect(assistantCalls()).toHaveLength(1))
  const body = lastBody()
  expect(body.prediction_id).toBe(5718)
  expect(body.messages).toEqual([{ role: 'user',
    content: 'Explain the confidence on Ja Morant 3PM 1.5 UNDER (game 2026-10-29)' }])
  await screen.findByText('ok')
})

it('on a phone the panel is a bottom sheet and each card has the explain button', async () => {
  screenWidth({ phone: true })
  render(<History />)
  const cards = await screen.findByRole('list', { name: 'Predictions' })
  const card = within(cards).getByText('Donovan Clingan').closest('li')
  fireEvent.click(within(card).getByRole('button', { name: 'Explain this pick' }))
  expect(screen.getByRole('complementary', { name: 'Ask' }).className).toContain('ask-sheet')
  await vi.waitFor(() => expect(lastBody().prediction_id).toBe(5719))
})

it('below 1280 px on desktop the panel is also a sheet', async () => {
  screenWidth({})
  render(<History />)
  await screen.findByText('Ja Morant')
  fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
  expect(screen.getByRole('complementary', { name: 'Ask' }).className).toContain('ask-sheet')
})

it('an example question sends straight away; follow-ups carry the history', async () => {
  screenWidth({ wide: true })
  render(<History />)
  await screen.findByText('Ja Morant')
  fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
  const panel = screen.getByRole('complementary', { name: 'Ask' })
  fireEvent.click(within(panel).getByRole('button', { name: /Ja Morant threes/ }))
  await within(panel).findByText('ok')
  fireEvent.change(within(panel).getByLabelText('Question'), { target: { value: 'and rebounds?' } })
  fireEvent.keyDown(within(panel).getByLabelText('Question'), { key: 'Enter' })
  await vi.waitFor(() => expect(assistantCalls()).toHaveLength(2))
  expect(lastBody().messages.map((m) => m.role)).toEqual(['user', 'assistant', 'user'])
})

it('shows the server’s reason when the assistant is unavailable', async () => {
  screenWidth({ wide: true })
  reply = json(503, { detail: 'OLLAMA_API_KEY is not set in backend/.env' })
  render(<History />)
  await screen.findByText('Ja Morant')
  fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
  const panel = screen.getByRole('complementary', { name: 'Ask' })
  fireEvent.change(within(panel).getByLabelText('Question'), { target: { value: 'hi' } })
  fireEvent.click(within(panel).getByRole('button', { name: 'Send' }))
  expect((await within(panel).findByRole('alert')).textContent).toMatch(/OLLAMA_API_KEY/)
})
