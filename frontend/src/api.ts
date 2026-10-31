// Thin fetch wrapper using the exact backend shapes:
//   POST /api/entry { text } -> draft Entry (confirmed = 0)
//   POST /api/transactions/confirm { id } -> confirmed Entry
//   GET /api/ledger -> Entry[] (confirmed only, newest first)
//   POST /api/transcribe (multipart audio) -> { text }
import type {
  CreditSummary,
  Entry,
  Explanation,
  PassportActivity,
  PassportSection,
  PassportSnapshot,
  ShopPassport,
  WeeklyBar,
} from './types'

export interface EditedValues {
  item: string
  quantity: number
  amount_kes: number
}

async function detailMessage(res: Response): Promise<string> {
  try {
    const data: unknown = await res.json()
    if (
      typeof data === 'object' &&
      data !== null &&
      'detail' in data &&
      typeof (data as { detail: unknown }).detail === 'string'
    ) {
      return (data as { detail: string }).detail
    }
  } catch {
    // Fall through to the generic message below.
  }
  return `Something went wrong (server said ${res.status}). Please try again.`
}

export async function postEntry(text: string): Promise<Entry> {
  let res: Response
  try {
    res = await fetch('/api/entry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    })
  } catch {
    throw new Error(
      'Cannot reach the server. Start the backend first, then try again.',
    )
  }
  if (res.status === 502) {
    throw new Error(
      "Spark couldn't understand that entry. Try something like “sold 5kg sugar for 800 bob”.",
    )
  }
  if (!res.ok) {
    throw new Error(await detailMessage(res))
  }
  return (await res.json()) as Entry
}

export async function confirmEntry(
  id: number,
  edited: EditedValues,
): Promise<Entry> {
  // The backend's ConfirmIn shape only reads `id`; the edited values ride
  // along so a future backend update can persist them without a frontend change.
  let res: Response
  try {
    res = await fetch('/api/transactions/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, ...edited }),
    })
  } catch {
    throw new Error(
      'Cannot reach the server. Your entry is saved as a draft — try confirming again.',
    )
  }
  if (!res.ok) {
    throw new Error(await detailMessage(res))
  }
  return (await res.json()) as Entry
}

/** Send a WAV voice clip; resolve with the transcript to review. Never auto-saves. */
export async function postTranscription(audio: Blob): Promise<string> {
  const form = new FormData()
  form.append('audio', audio, 'recording.wav')
  let res: Response
  try {
    res = await fetch('/api/transcribe', { method: 'POST', body: form })
  } catch {
    throw new Error(
      'Cannot reach the server. Start the backend first, then try again.',
    )
  }
  if (res.status === 413) {
    throw new Error(
      'That recording is too long. Keep it under 60 seconds and try again.',
    )
  }
  if (res.status === 502) {
    throw new Error(
      "Could not turn that into words. Speak clearly and try again.",
    )
  }
  if (!res.ok) {
    throw new Error(await detailMessage(res))
  }
  const data: unknown = await res.json()
  const text =
    typeof data === 'object' && data !== null && 'text' in data
      ? (data as { text: unknown }).text
      : null
  if (typeof text !== 'string' || !text.trim()) {
    throw new Error('Heard nothing usable — hold the mic closer and try again.')
  }
  return text
}

export async function fetchCreditSummary(): Promise<CreditSummary> {
  let res: Response
  try {
    res = await fetch('/api/credit/summary')
  } catch {
    throw new Error(
      'Cannot reach the server. Start the backend, then tap Retry.',
    )
  }
  if (res.status === 404) {
    throw new Error(
      'No demo data yet. Seed the demo shop first, then open this tab.',
    )
  }
  if (!res.ok) {
    throw new Error(await detailMessage(res))
  }
  return (await res.json()) as CreditSummary
}

export async function fetchWeekly(): Promise<WeeklyBar[]> {
  let res: Response
  try {
    res = await fetch('/api/dashboard/weekly')
  } catch {
    throw new Error(
      'Cannot reach the server. Start the backend, then tap Retry.',
    )
  }
  if (res.status === 404) {
    throw new Error(
      'No demo data yet. Seed the demo shop first, then open this tab.',
    )
  }
  if (!res.ok) {
    throw new Error(await detailMessage(res))
  }
  const data = (await res.json()) as { weeks: WeeklyBar[] }
  return data.weeks
}

export async function fetchExplanation(): Promise<Explanation> {
  let res: Response
  try {
    res = await fetch('/api/credit/explain')
  } catch {
    throw new Error(
      'Cannot reach the server. Start the backend, then tap Retry.',
    )
  }
  if (res.status === 404) {
    throw new Error(
      'No demo data yet. Seed the demo shop first, then open this tab.',
    )
  }
  if (!res.ok) {
    throw new Error(await detailMessage(res))
  }
  return (await res.json()) as Explanation
}

export async function fetchLedger(): Promise<Entry[]> {
  let res: Response
  try {
    res = await fetch('/api/ledger')
  } catch {
    throw new Error(
      'Cannot reach the server. Start the backend, then tap Retry.',
    )
  }
  if (!res.ok) {
    throw new Error(await detailMessage(res))
  }
  return (await res.json()) as Entry[]
}

// Business Passport calls. The raw share token is returned once by
// createPassport and must never be stored anywhere. Status errors carry
// the HTTP code so the lender page can show the right message per code.
export class ApiStatusError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export interface CreatedPassport {
  id: number
  token: string
  share_path: string
  expires_at: string
}

export async function createPassport(input: {
  lender_name: string
  expires_in_days: number
  sections: PassportSection[]
}): Promise<CreatedPassport> {
  let res: Response
  try {
    res = await fetch('/api/passports', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(input),
    })
  } catch {
    throw new Error(
      'Cannot reach the server. Start the backend, then try again.',
    )
  }
  if (!res.ok) {
    throw new Error(await detailMessage(res))
  }
  return (await res.json()) as CreatedPassport
}

export async function fetchPassports(): Promise<ShopPassport[]> {
  let res: Response
  try {
    res = await fetch('/api/passports')
  } catch {
    throw new Error(
      'Cannot reach the server. Start the backend, then tap Retry.',
    )
  }
  if (!res.ok) {
    throw new Error(await detailMessage(res))
  }
  return (await res.json()) as ShopPassport[]
}

export async function revokePassport(id: number): Promise<void> {
  let res: Response
  try {
    res = await fetch(`/api/passports/${id}/revoke`, { method: 'POST' })
  } catch {
    throw new Error(
      'Cannot reach the server. Start the backend, then try again.',
    )
  }
  if (!res.ok) {
    throw new Error(await detailMessage(res))
  }
}

export async function fetchPassportActivity(
  id: number,
): Promise<PassportActivity> {
  let res: Response
  try {
    res = await fetch(`/api/passports/${id}/activity`)
  } catch {
    throw new Error(
      'Cannot reach the server. Start the backend, then tap Retry.',
    )
  }
  if (!res.ok) {
    throw new Error(await detailMessage(res))
  }
  return (await res.json()) as PassportActivity
}

export async function fetchPublicPassport(
  token: string,
): Promise<PassportSnapshot> {
  let res: Response
  try {
    res = await fetch(`/api/p/${token}`)
  } catch {
    throw new ApiStatusError(0, 'network')
  }
  if (!res.ok) {
    throw new ApiStatusError(res.status, await detailMessage(res))
  }
  return (await res.json()) as PassportSnapshot
}
