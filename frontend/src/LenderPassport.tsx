// Public lender page at /p/<token>: no tab bar, no shopkeeper UI.
// Renders only the sections present in the snapshot.
import { useEffect, useState } from 'react'
import { ApiStatusError, fetchPublicPassport } from './api'
import { FactorBars, LoanCard, PerformanceSummary, ScoreRing } from './creditShared'
import { STR } from './strings'
import type { PassportSnapshot } from './types'

type Problem = 'invalid' | 'gone' | 'busy' | 'offline'

function formatDay(iso: string): string {
  return new Date(iso).toLocaleDateString('en-KE', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  })
}

export default function LenderPassport({ token }: { token: string }): JSX.Element {
  const [snapshot, setSnapshot] = useState<PassportSnapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [problem, setProblem] = useState<Problem | null>(null)

  async function load(): Promise<void> {
    setLoading(true)
    setProblem(null)
    try {
      setSnapshot(await fetchPublicPassport(token))
    } catch (err) {
      setSnapshot(null)
      if (err instanceof ApiStatusError) {
        if (err.status === 404) setProblem('invalid')
        else if (err.status === 410) setProblem('gone')
        else if (err.status === 429) setProblem('busy')
        else setProblem('offline')
      } else {
        setProblem('offline')
      }
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token])

  if (loading) {
    return <p className="message">{STR.lender.loading}</p>
  }

  if (problem === 'invalid') {
    return (
      <section className="card" aria-label={STR.lender.invalidTitle}>
        <h2>{STR.lender.invalidTitle}</h2>
        <p>{STR.lender.invalidBody}</p>
      </section>
    )
  }

  if (problem === 'gone') {
    return (
      <section className="card" aria-label={STR.lender.goneTitle}>
        <h2>{STR.lender.goneTitle}</h2>
        <p>{STR.lender.goneBody}</p>
      </section>
    )
  }

  if (problem === 'busy') {
    return (
      <section className="card" aria-label={STR.lender.busyTitle}>
        <h2>{STR.lender.busyTitle}</h2>
        <button type="button" className="btn secondary" onClick={() => void load()}>
          {STR.common.retry}
        </button>
      </section>
    )
  }

  if (problem === 'offline' || !snapshot) {
    return (
      <section className="card" aria-label={STR.lender.offlineTitle}>
        <h2>{STR.lender.offlineTitle}</h2>
        <p>{STR.lender.offlineBody}</p>
        <button type="button" className="btn secondary" onClick={() => void load()}>
          {STR.common.retry}
        </button>
      </section>
    )
  }

  return (
    <div>
      <section className="card" aria-label="Shop">
        <h1>{snapshot.shop_name}</h1>
        <p className="hint">{STR.lender.preparedOn} {formatDay(snapshot.generated_at)}</p>
        {snapshot.expires_at && (
          <p className="hint">{STR.lender.validUntil} {formatDay(snapshot.expires_at)}</p>
        )}
      </section>

      {snapshot.is_demo_shop && (
        <p className="demo-banner" role="note">
          {STR.app.demoBanner}
        </p>
      )}

      {snapshot.score &&
        (snapshot.score.score !== null && snapshot.score.band ? (
          <>
            <section className="card score-card" aria-label={STR.lender.scoreTitle}>
              <ScoreRing score={snapshot.score.score} band={snapshot.score.band} />
            </section>
            <section className="card" aria-label={STR.lender.factorsTitle}>
              <h2>{STR.lender.factorsTitle}</h2>
              <FactorBars factors={snapshot.score.factors} />
            </section>
          </>
        ) : (
          <section className="card" aria-label={STR.lender.noScoreTitle}>
            <h2>{STR.lender.noScoreTitle}</h2>
            <p className="message">{STR.lender.noScore}</p>
          </section>
        ))}

      {snapshot.loan && (
        <section className="card" aria-label={STR.lender.loanTitle}>
          <h2>{STR.lender.loanTitle}</h2>
          <LoanCard loan={snapshot.loan} />
        </section>
      )}

      {snapshot.performance && (
        <section className="card" aria-label={STR.lender.performanceTitle}>
          <h2>{STR.lender.performanceTitle}</h2>
          <PerformanceSummary performance={snapshot.performance} />
        </section>
      )}

      <p className="disclaimer">{snapshot.disclaimer}</p>
      <footer className="lender-footer">
        {STR.lender.sharedWithConsent}
      </footer>
    </div>
  )
}
