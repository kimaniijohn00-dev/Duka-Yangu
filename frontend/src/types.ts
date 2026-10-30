// Entry shapes mirror the FastAPI backend exactly:
// POST /api/entry -> Entry draft (confirmed = 0)
// POST /api/transactions/confirm -> Entry (confirmed = 1)
// GET /api/ledger -> Entry[] (confirmed only, newest first)
// GET /api/credit/summary -> CreditSummary
// GET /api/dashboard/weekly -> { weeks: WeeklyBar[] }

export type EntryType =
  | 'sale'
  | 'purchase'
  | 'expense'
  | 'credit_given'
  | 'credit_received'

export interface CreditFactor {
  name: string
  points: number
  max_points: number
  value: number | null
  explanation: string
}

export type CreditStatus = 'scored' | 'insufficient_data'
export type CreditBand = 'Building' | 'Developing' | 'Ready' | 'Strong'

export interface CreditSummary {
  shop_id: number
  shop_name: string
  as_of: string
  status: CreditStatus
  explanation: string
  score: number | null
  band: CreditBand | null
  factors: CreditFactor[]
  safe_loan_kes: number
  monthly_repayment_kes: number
  term_months: number
  loan_reasoning?: string
  tips: string[]
  disclaimer: string
}

export interface WeeklyBar {
  week_start: string
  sales_kes: number
  purchases_kes: number
  expenses_kes: number
}

export interface Explanation {
  text: string
  source: 'spark' | 'template'
}

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
