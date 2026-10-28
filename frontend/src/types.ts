// Entry shapes mirror the FastAPI backend exactly:
// POST /api/entry -> Entry draft (confirmed = 0)
// POST /api/transactions/confirm -> Entry (confirmed = 1)
// GET /api/ledger -> Entry[] (confirmed only, newest first)

export type EntryType =
  | 'sale'
  | 'purchase'
  | 'expense'
  | 'credit_given'
  | 'credit_received'

export interface Entry {
  id: number
  shop_id: number | null
  type: EntryType
  item: string
  quantity: number
  unit: string
  amount_kes: number
  counterparty: string | null
  raw_text: string
  source: string
  confirmed: number
  occurred_at: string
}
