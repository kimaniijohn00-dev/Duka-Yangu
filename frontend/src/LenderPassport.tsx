// Public lender page at /p/<token>: no tab bar, no shopkeeper UI.
// Renders only the sections present in the snapshot.
import { useEffect, useState } from 'react'
import { ApiStatusError, fetchPublicPassport } from './api'
import { FactorBars, LoanCard, PerformanceSummary, ScoreRing } from './creditShared'
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
    return <p className="message">Loading this passport…</p>
  }

  if (problem === 'invalid') {
    return (
      <section className="card" aria-label="Invalid link">
        <h2>This link isn&apos;t valid.</h2>
        <p>Check the link and try again.</p>
      </section>
    )
  }

  if (problem === 'gone') {
    return (
      <section className="card" aria-label="Unavailable passport">
        <h2>This passport is no longer available.</h2>
        <p>Ask the shopkeeper for a new link.</p>
      </section>
    )
  }

  if (problem === 'busy') {
    return (
      <section className="card" aria-label="Too many requests">
        <h2>Too many requests, try again in a minute.</h2>
        <button type="button" className="btn secondary" onClick={() => void load()}>
          Retry
        </button>
      </section>
    )
  }

  if (problem === 'offline' || !snapshot) {
    return (
      <section className="card" aria-label="Connection problem">
        <h2>Cannot reach the server.</h2>
        <p>Check your connection, then try again.</p>
        <button type="button" className="btn secondary" onClick={() => void load()}>
          Retry
        </button>
      </section>
    )
  }

  return (
    <div>
      <section className="card" aria-label="Shop">
        <h2>{snapshot.shop_name}</h2>
        <p className="hint">Prepared on {formatDay(snapshot.generated_at)}</p>
        {snapshot.expires_at && (
          <p className="hint">Valid until {formatDay(snapshot.expires_at)}</p>
        )}
      </section>

      {snapshot.is_demo_shop && (
        <p className="demo-banner" role="note">
          Demo shop with synthetic data
        </p>
      )}

      {snapshot.score &&
        (snapshot.score.score !== null && snapshot.score.band ? (
          <>
            <section className="card score-card" aria-label="Credit score">
              <ScoreRing score={snapshot.score.score} band={snapshot.score.band} />
            </section>
            <section className="card" aria-label="Score factors">
              <h2>What makes the score</h2>
              <FactorBars factors={snapshot.score.factors} />
            </section>
          </>
        ) : (
          <section className="card" aria-label="Credit score">
            <h2>Score</h2>
            <p className="message">Not enough history to score this shop yet.</p>
          </section>
        ))}

      {snapshot.loan && (
        <section className="card" aria-label="Safe loan estimate">
          <h2>Safe loan estimate</h2>
          <LoanCard loan={snapshot.loan} />
        </section>
      )}

      {snapshot.performance && (
        <section className="card" aria-label="Performance summary">
          <h2>Performance summary</h2>
          <PerformanceSummary performance={snapshot.performance} />
        </section>
      )}

      <p className="disclaimer">{snapshot.disclaimer}</p>
      <footer className="lender-footer">
        Shared with the shopkeeper&apos;s consent. Duka-Yangu
      </footer>
    </div>
  )
}
