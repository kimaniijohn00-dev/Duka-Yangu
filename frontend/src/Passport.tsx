import { useCallback, useEffect, useState } from 'react'
import {
  createPassport,
  fetchPassportActivity,
  fetchPassports,
  revokePassport,
} from './api'
import type {
  PassportActivity,
  PassportSection,
  ShopPassport,
} from './types'

const SECTION_LABELS: Record<PassportSection, string> = {
  score: 'Score',
  loan: 'Loan estimate',
  performance: 'Performance summary',
}

const SECTION_PHRASES: Record<PassportSection, string> = {
  score: 'your score',
  loan: 'your loan estimate',
  performance: 'a performance summary',
}

const SECTION_ORDER: PassportSection[] = ['score', 'loan', 'performance']

function joinPhrases(parts: string[]): string {
  if (parts.length === 1) return parts[0]
  return `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}`
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString('en-KE', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  })
}

function formatWhen(iso: string): string {
  return new Date(iso).toLocaleString('en-KE', {
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    // Older browsers: fall back to a temporary textarea.
    try {
      const area = document.createElement('textarea')
      area.value = text
      document.body.appendChild(area)
      area.select()
      const ok = document.execCommand('copy')
      document.body.removeChild(area)
      return ok
    } catch {
      return false
    }
  }
}

export default function Passport(): JSX.Element {
  const [lender, setLender] = useState('')
  const [shareScore, setShareScore] = useState(true)
  const [shareLoan, setShareLoan] = useState(true)
  const [sharePerf, setSharePerf] = useState(true)
  const [days, setDays] = useState<7 | 30>(7)
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  // The fresh link lives in memory only: never stored, cleared on dismiss.
  const [freshLink, setFreshLink] = useState<{ url: string; lender: string } | null>(null)
  const [copied, setCopied] = useState(false)

  const [passports, setPassports] = useState<ShopPassport[]>([])
  const [listLoading, setListLoading] = useState(true)
  const [listError, setListError] = useState<string | null>(null)

  const [confirmRevokeId, setConfirmRevokeId] = useState<number | null>(null)
  const [revoking, setRevoking] = useState(false)

  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [activity, setActivity] = useState<PassportActivity | null>(null)
  const [activityLoading, setActivityLoading] = useState(false)
  const [activityError, setActivityError] = useState<string | null>(null)

  const sections: PassportSection[] = SECTION_ORDER.filter((s) =>
    s === 'score' ? shareScore : s === 'loan' ? shareLoan : sharePerf,
  )
  const lenderName = lender.trim()
  const lenderValid = lenderName.length >= 1 && lenderName.length <= 60
  const canCreate = lenderValid && sections.length > 0 && !creating

  const consentSentence =
    sections.length === 0
      ? 'Tick at least one thing to share before creating the link.'
      : `You are sharing ${joinPhrases(sections.map((s) => SECTION_PHRASES[s]))} ` +
        `with ${lenderName || 'your lender'} for ${days} days. ` +
        'They will not see individual sales or customer names. ' +
        'You can stop this at any time.'

  const loadList = useCallback(async () => {
    setListLoading(true)
    setListError(null)
    try {
      setPassports(await fetchPassports())
    } catch (err) {
      setListError(
        err instanceof Error ? err.message : 'Could not load passports.',
      )
    } finally {
      setListLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadList()
  }, [loadList])

  async function handleCreate(): Promise<void> {
    if (!canCreate) return
    setCreating(true)
    setCreateError(null)
    try {
      const created = await createPassport({
        lender_name: lenderName,
        expires_in_days: days,
        sections,
      })
      // Token stays in memory only: it is shown once below, never listed.
      setFreshLink({
        url: window.location.origin + created.share_path,
        lender: lenderName,
      })
      setCopied(false)
      await loadList()
    } catch (err) {
      setCreateError(
        err instanceof Error ? err.message : 'Could not create the link.',
      )
    } finally {
      setCreating(false)
    }
  }

  async function handleCopy(): Promise<void> {
    if (!freshLink) return
    setCopied(await copyText(freshLink.url))
  }

  async function handleRevoke(id: number): Promise<void> {
    setRevoking(true)
    try {
      await revokePassport(id)
      setConfirmRevokeId(null)
      if (expandedId === id) {
        setExpandedId(null)
        setActivity(null)
      }
      await loadList()
    } catch (err) {
      setListError(
        err instanceof Error ? err.message : 'Could not stop sharing.',
      )
    } finally {
      setRevoking(false)
    }
  }

  async function toggleExpanded(id: number): Promise<void> {
    if (expandedId === id) {
      setExpandedId(null)
      return
    }
    setExpandedId(id)
    setActivity(null)
    setActivityError(null)
    setActivityLoading(true)
    try {
      setActivity(await fetchPassportActivity(id))
    } catch (err) {
      setActivityError(
        err instanceof Error ? err.message : 'Could not load views.',
      )
    } finally {
      setActivityLoading(false)
    }
  }

  const sorted = [...passports].sort((a, b) => {
    const rank = (s: string): number => (s === 'active' ? 0 : 1)
    return rank(a.status) - rank(b.status) || b.id - a.id
  })

  return (
    <div>
      <section className="card" aria-label="Share a passport">
        <h2>Share a passport</h2>
        <label className="field-label" htmlFor="lender-name">
          Lender name
        </label>
        <input
          id="lender-name"
          className="text-input"
          type="text"
          maxLength={60}
          placeholder="e.g. KCB"
          value={lender}
          onChange={(e) => setLender(e.target.value)}
          disabled={creating}
        />
        <fieldset className="check-group">
          <legend className="field-label">What to share</legend>
          {SECTION_ORDER.map((section) => (
            <label className="check-row" key={section}>
              <input
                type="checkbox"
                checked={
                  section === 'score'
                    ? shareScore
                    : section === 'loan'
                      ? shareLoan
                      : sharePerf
                }
                onChange={(e) => {
                  const on = e.target.checked
                  if (section === 'score') setShareScore(on)
                  else if (section === 'loan') setShareLoan(on)
                  else setSharePerf(on)
                }}
                disabled={creating}
              />
              {SECTION_LABELS[section]}
            </label>
          ))}
        </fieldset>
        <fieldset className="check-group">
          <legend className="field-label">Link lasts</legend>
          <label className="check-row" key="days-7">
            <input
              type="radio"
              name="passport-days"
              checked={days === 7}
              onChange={() => setDays(7)}
              disabled={creating}
            />
            7 days
          </label>
          <label className="check-row" key="days-30">
            <input
              type="radio"
              name="passport-days"
              checked={days === 30}
              onChange={() => setDays(30)}
              disabled={creating}
            />
            30 days
          </label>
        </fieldset>
        <p className="consent" aria-live="polite">
          {consentSentence}
        </p>
        {createError && (
          <p className="message error" role="alert">
            {createError}
          </p>
        )}
        <div className="row">
          <button
            type="button"
            className="btn primary"
            onClick={() => void handleCreate()}
            disabled={!canCreate}
          >
            {creating ? 'Creating…' : 'Create passport link'}
          </button>
        </div>
      </section>

      {freshLink && (
        <section className="card link-card" aria-label="Your new link" aria-live="polite">
          <h2>Link ready for {freshLink.lender}</h2>
          <p className="link-once">
            This link is shown only once. Copy it now.
          </p>
          <p className="link-box">{freshLink.url}</p>
          <div className="row">
            <button
              type="button"
              className="btn secondary"
              onClick={() => void handleCopy()}
            >
              {copied ? 'Copied!' : 'Copy'}
            </button>
            <a
              className="btn secondary"
              href={`https://wa.me/?text=${encodeURIComponent(
                `Hello ${freshLink.lender}, here is my Duka-Yangu business passport: ${freshLink.url}`,
              )}`}
              target="_blank"
              rel="noreferrer"
            >
              Send on WhatsApp
            </a>
          </div>
          <button
            type="button"
            className="btn linklike"
            onClick={() => setFreshLink(null)}
          >
            Done — hide this link
          </button>
        </section>
      )}

      <section className="card" aria-label="Shared passports">
        <h2>Shared passports</h2>
        {listLoading && <p className="message">Loading passports…</p>}
        {!listLoading && listError && (
          <div>
            <p className="message error" role="alert">
              {listError}
            </p>
            <button
              type="button"
              className="btn secondary"
              onClick={() => void loadList()}
            >
              Retry
            </button>
          </div>
        )}
        {!listLoading && !listError && sorted.length === 0 && (
          <p className="message">
            No passports yet — share your first one above.
          </p>
        )}
        {!listLoading && !listError && sorted.length > 0 && (
          <ul className="passport-list">
            {sorted.map((p) => (
              <li key={p.id} className="passport-row">
                <button
                  type="button"
                  className="passport-main"
                  onClick={() => void toggleExpanded(p.id)}
                  aria-expanded={expandedId === p.id}
                >
                  <span className="passport-top">
                    <strong>{p.lender_name}</strong>
                    <span className={`badge status-${p.status}`}>
                      {p.status === 'active'
                        ? 'Active'
                        : p.status === 'expired'
                          ? 'Expired'
                          : 'Revoked'}
                    </span>
                  </span>
                  <span className="passport-sub">
                    {p.scope.map((s) => SECTION_LABELS[s]).join(' · ')}
                  </span>
                  <span className="passport-sub">
                    Expires {formatDate(p.expires_at)} · Viewed{' '}
                    {p.view_count === 1 ? 'once' : `${p.view_count} times`}
                    {p.last_viewed_at
                      ? `, last ${formatWhen(p.last_viewed_at)}`
                      : ''}
                  </span>
                </button>

                {expandedId === p.id && (
                  <div className="passport-detail">
                    {activityLoading && (
                      <p className="message">Loading views…</p>
                    )}
                    {!activityLoading && activityError && (
                      <p className="message error" role="alert">
                        {activityError}
                      </p>
                    )}
                    {!activityLoading &&
                      !activityError &&
                      activity &&
                      (activity.views.length === 0 ? (
                        <p className="message">
                          No views yet — the lender has not opened the link.
                        </p>
                      ) : (
                        <ul className="activity-list">
                          {activity.views.map((viewedAt, index) => (
                            <li key={`${viewedAt}-${index}`}>
                              Viewed {formatWhen(viewedAt)}
                            </li>
                          ))}
                        </ul>
                      ))}
                  </div>
                )}

                {p.status === 'active' &&
                  (confirmRevokeId === p.id ? (
                    <div className="confirm-box">
                      <p>
                        Stop sharing with {p.lender_name}? The link will stop
                        working immediately.
                      </p>
                      <div className="row">
                        <button
                          type="button"
                          className="btn primary"
                          onClick={() => void handleRevoke(p.id)}
                          disabled={revoking}
                        >
                          {revoking ? 'Stopping…' : 'Yes, stop sharing'}
                        </button>
                        <button
                          type="button"
                          className="btn secondary"
                          onClick={() => setConfirmRevokeId(null)}
                          disabled={revoking}
                        >
                          Keep sharing
                        </button>
                      </div>
                    </div>
                  ) : (
                    <button
                      type="button"
                      className="btn secondary"
                      onClick={() => setConfirmRevokeId(p.id)}
                    >
                      Revoke
                    </button>
                  ))}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
