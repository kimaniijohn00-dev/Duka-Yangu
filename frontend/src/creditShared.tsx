// Shared credit visuals: the Credit tab and the lender passport page render
// the same score ring, factor bars and loan card from these components.
import { formatKES } from './format'
import type {
  CreditFactor,
  PassportPerformance,
  PassportWeeklySale,
} from './types'

const BAND_COLORS: Record<string, string> = {
  Building: '#b23c17',
  Developing: '#96690a',
  Ready: '#1b6b3a',
  Strong: '#0d4d26',
}

export function ScoreRing({
  score,
  band,
}: {
  score: number
  band: string
}): JSX.Element {
  const radius = 60
  const circumference = 2 * Math.PI * radius
  const done = (Math.min(100, Math.max(0, score)) / 100) * circumference
  const color = BAND_COLORS[band] ?? '#1b6b3a'
  return (
    <div className="score-wrap">
      <svg
        className="score-ring"
        width="150"
        height="150"
        viewBox="0 0 150 150"
        role="img"
        aria-label={`Credit score ${score} out of 100, band ${band}`}
      >
        <circle cx="75" cy="75" r={radius} fill="none" stroke="#e3e9e3" strokeWidth="14" />
        <circle
          cx="75"
          cy="75"
          r={radius}
          fill="none"
          stroke={color}
          strokeWidth="14"
          strokeLinecap="round"
          strokeDasharray={`${done} ${circumference}`}
          transform="rotate(-90 75 75)"
        />
        <text x="75" y="74" textAnchor="middle" className="score-number">
          {score}
        </text>
        <text x="75" y="94" textAnchor="middle" className="score-denominator">
          / 100
        </text>
      </svg>
      <p className="band" style={{ color }}>
        {band}
      </p>
    </div>
  )
}

export function FactorBars({ factors }: { factors: CreditFactor[] }): JSX.Element {
  return (
    <div>
      {factors.map((factor) => (
        <div className="factor" key={factor.name}>
          <div className="factor-top">
            <span className="factor-name">{factor.name}</span>
            <span className="factor-points">
              {factor.points}/{factor.max_points}
            </span>
          </div>
          <div
            className="bar"
            role="img"
            aria-label={`${factor.name}: ${factor.points} of ${factor.max_points} points`}
          >
            <div
              className="fill"
              style={{ width: `${(factor.points / factor.max_points) * 100}%` }}
            />
          </div>
          <p className="factor-expl">{factor.explanation}</p>
        </div>
      ))}
    </div>
  )
}

export interface LoanFigures {
  safe_loan_kes: number
  monthly_repayment_kes: number
  term_months: number
  loan_reasoning?: string
}

export function LoanCard({ loan }: { loan: LoanFigures }): JSX.Element {
  return (
    <div>
      <p className="loan-big">{formatKES(loan.safe_loan_kes)}</p>
      <p>
        about {formatKES(loan.monthly_repayment_kes)} a month for{' '}
        {loan.term_months} months
      </p>
      {loan.loan_reasoning && <p className="hint">{loan.loan_reasoning}</p>}
    </div>
  )
}

export function weekLabel(iso: string): string {
  const date = new Date(`${iso}T00:00:00`)
  return `${date.getDate()}/${date.getMonth() + 1}`
}

function SalesBars({ weeks }: { weeks: PassportWeeklySale[] }): JSX.Element {
  const max = Math.max(1, ...weeks.map((w) => w.sales_kes))
  const height = 120
  const barWidth = 22
  const gap = 10
  const width = weeks.length * (barWidth + gap)
  return (
    <svg
      width={width}
      height={height + 22}
      viewBox={`0 0 ${width} ${height + 22}`}
      role="img"
      aria-label="Weekly sales for the last 8 weeks"
    >
      {weeks.map((week, index) => {
        const barH = Math.round((week.sales_kes / max) * height)
        const x = index * (barWidth + gap)
        return (
          <g key={week.week_start}>
            <rect
              x={x}
              y={height - barH}
              width={barWidth}
              height={barH}
              rx="3"
              fill="#1b6b3a"
            >
              <title>{`Week of ${week.week_start}: sales ${formatKES(week.sales_kes)}`}</title>
            </rect>
            <text x={x + barWidth / 2} y={height + 16} textAnchor="middle" className="chart-label">
              {weekLabel(week.week_start)}
            </text>
          </g>
        )
      })}
    </svg>
  )
}

export function PerformanceSummary({
  performance,
}: {
  performance: PassportPerformance
}): JSX.Element {
  return (
    <div>
      <div className="totals">
        <div className="total">
          <span className="total-label">Sales, 60 days</span>
          <strong>{formatKES(performance.total_sales_kes)}</strong>
        </div>
        <div className="total">
          <span className="total-label">Stock, 60 days</span>
          <strong>{formatKES(performance.total_purchases_kes)}</strong>
        </div>
      </div>
      <div className="totals">
        <div className="total">
          <span className="total-label">Costs, 60 days</span>
          <strong>{formatKES(performance.total_expenses_kes)}</strong>
        </div>
        <div className="total">
          <span className="total-label">Net margin</span>
          <strong>{performance.net_margin_pct}%</strong>
        </div>
      </div>
      <p>
        Outstanding customer credit:{' '}
        <strong>{formatKES(performance.outstanding_credit_kes)}</strong>
      </p>
      <figure className="chart">
        <div className="legend">
          <span className="legend-item">
            <span className="swatch sales" aria-hidden="true" /> Weekly sales
          </span>
        </div>
        <SalesBars weeks={performance.weekly_sales_kes} />
      </figure>
    </div>
  )
}
