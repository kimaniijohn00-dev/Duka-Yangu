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

// Business Passport shapes mirror the FastAPI backend exactly:
// POST /api/passports -> { id, token, share_path, expires_at }
// GET /api/passports -> ShopPassport[] (never tokens or hashes)
// GET /api/passports/{id}/activity -> { passport_id, views }
// GET /api/p/{token} -> PassportSnapshot (only the chosen sections)

export type PassportSection = 'score' | 'loan' | 'performance'

export interface PassportScore {
  score: number | null
  band: string | null
  factors: CreditFactor[]
}

export interface PassportLoan {
  safe_loan_kes: number
  monthly_repayment_kes: number
  term_months: number
  loan_reasoning: string
}

export interface PassportWeeklySale {
  week_start: string
  sales_kes: number
}

export interface PassportPerformance {
  total_sales_kes: number
  total_purchases_kes: number
  total_expenses_kes: number
  net_margin_pct: number
  outstanding_credit_kes: number
  weekly_sales_kes: PassportWeeklySale[]
}

export interface PassportSnapshot {
  shop_name: string
  generated_at: string
  as_of?: string
  is_demo_shop: boolean
  demo_note?: string
  disclaimer: string
  expires_at?: string
  score?: PassportScore
  loan?: PassportLoan
  performance?: PassportPerformance
}

export type PassportStatus = 'active' | 'expired' | 'revoked'

export interface ShopPassport {
  id: number
  lender_name: string
  scope: PassportSection[]
  created_at: string
  expires_at: string
  status: PassportStatus
  view_count: number
  last_viewed_at: string | null
}

export interface PassportActivity {
  passport_id: number
  views: string[]
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
