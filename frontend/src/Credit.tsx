import { useEffect, useState } from 'react'
import { fetchCreditSummary, fetchExplanation, fetchWeekly, isUnauthorized } from './api'
import { FactorAccordion, LoanCard, ScoreRing, weekLabel } from './creditShared'
import { formatKES } from './format'
import { STR } from './strings'
import type { CreditSummary, Explanation, WeeklyBar } from './types'

interface CreditProps {
  /** True when shown to a visitor without a login (public demo data). */
  demo: boolean
  /** Jump to the login or register screen from the demo prompts. */
  onAuthRequest: (mode: 'login' | 'register') => void
  /** Called when the session is rejected mid-use (expired cookie). */
  onUnauthorized: () => void
}

const DISCLAIMER = STR.credit.disclaimerFallback

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
          <span className="swatch sales" aria-hidden="true" /> {STR.credit.salesLegend}
        </span>
        <span className="legend-item">
          <span className="swatch costs" aria-hidden="true" /> {STR.credit.costsLegend}
        </span>
      </div>
      <svg
        width={width}
        height={height + 22}
        viewBox={`0 0 ${width} ${height + 22}`}
        role="img"
        aria-label={STR.credit.weeklyChartLabel}
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
                style={{ fill: 'var(--green-ink)' }}
              >
                <title>{`Week of ${week.week_start}: sales ${formatKES(week.sales_kes)}`}</title>
              </rect>
              <rect
                x={x + barWidth + inGroupGap}
                y={height - costsH}
                width={barWidth}
                height={costsH}
                rx="3"
                style={{ fill: 'var(--muted)' }}
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

export default function Credit({ demo, onAuthRequest, onUnauthorized }: CreditProps): JSX.Element {
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
      if (isUnauthorized(err)) {
        onUnauthorized()
        return
      }
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
      if (isUnauthorized(err)) {
        onUnauthorized()
        return
      }
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
    <div className="credit-layout">
      {demo && (
        <p className="demo-banner" role="note">
          {STR.app.demoBanner}{' '}
          <button type="button" className="btn linklike" onClick={() => onAuthRequest('login')}>
            {STR.app.login}
          </button>
        </p>
      )}
      {demo && (
        <section className="card" aria-label={STR.credit.loginTitle}>
          <h2>{STR.credit.loginTitle}</h2>
          <p>{STR.credit.loginBody}</p>
          <div className="row">
            <button
              type="button"
              className="btn primary"
              onClick={() => onAuthRequest('login')}
            >
              {STR.app.login}
            </button>
            <button
              type="button"
              className="btn secondary"
              onClick={() => onAuthRequest('register')}
            >
              {STR.credit.register}
            </button>
          </div>
        </section>
      )}

      {loading && (
        <section className="card" aria-label={STR.credit.loading}>
          <p className="skeleton skeleton-big" aria-hidden="true">…</p>
          <p className="skeleton skeleton-text" aria-hidden="true">…</p>
          <p className="skeleton skeleton-text" aria-hidden="true">…</p>
        </section>
      )}

      {!loading && error && (
        <div>
          <p className="message error" role="alert">
            {error}
          </p>
          <button type="button" className="btn secondary" onClick={() => void load()}>
            {STR.common.retry}
          </button>
        </div>
      )}

      {!loading && !error && summary && summary.status === 'insufficient_data' && (
        <section className="card" aria-label={STR.credit.emptyTitle}>
          <h2>{STR.credit.emptyTitle}</h2>
          <div className="empty-state">
            <p>{summary.explanation}</p>
          </div>
          {summary.tips.length > 0 && <p className="hint">{summary.tips[0]}</p>}
        </section>
      )}

      {!loading &&
        !error &&
        summary &&
        summary.status === 'scored' &&
        summary.score !== null &&
        summary.band && (
          <div className="credit-grid">
            <div className="credit-primary">
              <section className="card score-card" aria-label={STR.credit.scoreTitle}>
                <ScoreRing score={summary.score} band={summary.band} />
                <p className="hint">{summary.explanation}</p>
              </section>

              <section className="card" aria-label={STR.credit.loanTitle}>
                <h2>{STR.credit.loanTitle}</h2>
                <LoanCard loan={summary} />
              </section>
            </div>

            <div className="credit-secondary">
              <section className="card" aria-label={STR.credit.factorsTitle}>
                <h2>{STR.credit.factorsTitle}</h2>
                <FactorAccordion factors={summary.factors} />
              </section>

              <section className="card" aria-label={STR.credit.plainTitle}>
                <h2>{STR.credit.plainTitle}</h2>
                {explLoading && <p className="message">{STR.credit.aiLoading}</p>}
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
                      {STR.common.retry}
                    </button>
                  </div>
                )}
                {!explLoading && !explError && explanation && (
                  <div>
                    <p>{explanation.text}</p>
                    <p className="hint">
                      {explanation.source === 'spark'
                        ? STR.credit.aiNote
                        : STR.credit.standardNote}
                    </p>
                  </div>
                )}
              </section>

              <section className="card" aria-label={STR.credit.tipsTitle}>
                <h2>{STR.credit.tipsTitle}</h2>
                <ul className="tips">
                  {summary.tips.map((tip) => (
                    <li key={tip}>{tip}</li>
                  ))}
                </ul>
              </section>

              <section className="card" aria-label={STR.credit.weeksTitle}>
                <h2>{STR.credit.weeksTitle}</h2>
                {weeks && weeks.length > 0 ? (
                  <WeeklyChart weeks={weeks} />
                ) : (
                  <div className="empty-state">
                    <p>{STR.credit.noWeeks}</p>
                  </div>
                )}
              </section>
            </div>
          </div>
        )}

      <p className="disclaimer">{summary?.disclaimer ?? DISCLAIMER}</p>
    </div>
  )
}
