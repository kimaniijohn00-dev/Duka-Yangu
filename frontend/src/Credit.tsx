import { useEffect, useState } from 'react'
import { fetchCreditSummary, fetchExplanation, fetchWeekly } from './api'
import { formatKES } from './format'
import type { CreditSummary, Explanation, WeeklyBar } from './types'

const BAND_COLORS: Record<string, string> = {
  Building: '#b23c17',
  Developing: '#96690a',
  Ready: '#1b6b3a',
  Strong: '#0d4d26',
}

const DISCLAIMER = 'Estimate only, not a lending decision.'

function ScoreRing({ score, band }: { score: number; band: string }): JSX.Element {
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

function weekLabel(iso: string): string {
  const date = new Date(`${iso}T00:00:00`)
  return `${date.getDate()}/${date.getMonth() + 1}`
}

function WeeklyChart({ weeks }: { weeks: WeeklyBar[] }): JSX.Element {
  const costs = (week: WeeklyBar): number => week.purchases_kes + week.expenses_kes
  const max = Math.max(1, ...weeks.map((w) => Math.max(w.sales_kes, costs(w))))
  const height = 140
  const barWidth = 13
  const inGroupGap = 4
  const groupGap = 12
  const groupWidth = barWidth * 2 + inGroupGap + groupGap
  const width = weeks.length * groupWidth
  const barHeight = (value: number): number => Math.round((value / max) * height)
  return (
    <figure className="chart">
      <div className="legend">
        <span className="legend-item">
          <span className="swatch sales" aria-hidden="true" /> Sales
        </span>
        <span className="legend-item">
          <span className="swatch costs" aria-hidden="true" /> Stock + costs
        </span>
      </div>
      <svg
        width={width}
        height={height + 22}
        viewBox={`0 0 ${width} ${height + 22}`}
        role="img"
        aria-label="Weekly sales against stock and costs for the last 8 weeks"
      >
        {weeks.map((week, index) => {
          const x = index * groupWidth
          const salesH = barHeight(week.sales_kes)
          const costsH = barHeight(costs(week))
          return (
            <g key={week.week_start}>
              <rect
                x={x}
                y={height - salesH}
                width={barWidth}
                height={salesH}
                rx="3"
                fill="#1b6b3a"
              >
                <title>{`Week of ${week.week_start}: sales ${formatKES(week.sales_kes)}`}</title>
              </rect>
              <rect
                x={x + barWidth + inGroupGap}
                y={height - costsH}
                width={barWidth}
                height={costsH}
                rx="3"
                fill="#9aa39b"
              >
                <title>{`Week of ${week.week_start}: stock and costs ${formatKES(costs(week))}`}</title>
              </rect>
              <text x={x + barWidth} y={height + 16} textAnchor="middle" className="chart-label">
                {weekLabel(week.week_start)}
              </text>
            </g>
          )
        })}
      </svg>
    </figure>
  )
}

export default function Credit(): JSX.Element {
  const [summary, setSummary] = useState<CreditSummary | null>(null)
  const [weeks, setWeeks] = useState<WeeklyBar[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [explanation, setExplanation] = useState<Explanation | null>(null)
  const [explLoading, setExplLoading] = useState(true)
  const [explError, setExplError] = useState<string | null>(null)

  async function load(): Promise<void> {
    setLoading(true)
    setError(null)
    try {
      const [credit, series] = await Promise.all([fetchCreditSummary(), fetchWeekly()])
      setSummary(credit)
      setWeeks(series)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load the dashboard.')
    } finally {
      setLoading(false)
    }
  }

  async function loadExplanation(): Promise<void> {
    setExplLoading(true)
    setExplError(null)
    try {
      setExplanation(await fetchExplanation())
    } catch (err) {
      setExplError(
        err instanceof Error ? err.message : 'Could not load the explanation.',
      )
    } finally {
      setExplLoading(false)
    }
  }

  useEffect(() => {
    void load()
    void loadExplanation()
  }, [])

  return (
    <div>
      <p className="demo-banner" role="note">
        Demo shop with synthetic data
      </p>

      {loading && <p className="message">Checking credit readiness…</p>}

      {!loading && error && (
        <div>
          <p className="message error" role="alert">
            {error}
          </p>
          <button type="button" className="btn secondary" onClick={() => void load()}>
            Retry
          </button>
        </div>
      )}

      {!loading && !error && summary && summary.status === 'insufficient_data' && (
        <section className="card" aria-label="Not enough history">
          <h2>Not enough history yet</h2>
          <p>{summary.explanation}</p>
          {summary.tips.length > 0 && <p className="hint">{summary.tips[0]}</p>}
        </section>
      )}

      {!loading &&
        !error &&
        summary &&
        summary.status === 'scored' &&
        summary.score !== null &&
        summary.band && (
          <>
            <section className="card score-card" aria-label="Credit score">
              <ScoreRing score={summary.score} band={summary.band} />
              <p className="hint">{summary.explanation}</p>
            </section>

            <section className="card" aria-label="Score factors">
              <h2>What makes the score</h2>
              {summary.factors.map((factor) => (
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
            </section>

            <section className="card" aria-label="Safe loan estimate">
              <h2>Safe loan estimate</h2>
              <p className="loan-big">{formatKES(summary.safe_loan_kes)}</p>
              <p>
                about {formatKES(summary.monthly_repayment_kes)} a month for{' '}
                {summary.term_months} months
              </p>
              {summary.loan_reasoning && (
                <p className="hint">{summary.loan_reasoning}</p>
              )}
            </section>

            <section className="card" aria-label="In plain English">
              <h2>In plain English</h2>
              {explLoading && <p className="message">Asking Spark to explain…</p>}
              {!explLoading && explError && (
                <div>
                  <p className="message error" role="alert">
                    {explError}
                  </p>
                  <button
                    type="button"
                    className="btn secondary"
                    onClick={() => void loadExplanation()}
                  >
                    Retry
                  </button>
                </div>
              )}
              {!explLoading && !explError && explanation && (
                <div>
                  <p>{explanation.text}</p>
                  <p className="hint">
                    {explanation.source === 'spark'
                      ? 'Written by AI from the numbers above.'
                      : 'Standard summary.'}
                  </p>
                </div>
              )}
            </section>

            <section className="card" aria-label="What would raise your score">
              <h2>What would raise your score</h2>
              <ul className="tips">
                {summary.tips.map((tip) => (
                  <li key={tip}>{tip}</li>
                ))}
              </ul>
            </section>

            <section className="card" aria-label="Last 8 weeks">
              <h2>Last 8 weeks</h2>
              {weeks && weeks.length > 0 ? (
                <WeeklyChart weeks={weeks} />
              ) : (
                <p className="message">No weekly data yet.</p>
              )}
            </section>
          </>
        )}

      <p className="disclaimer">{summary?.disclaimer ?? DISCLAIMER}</p>
    </div>
  )
}
