import { useCallback, useEffect, useState } from 'react'
import {
  createPassport,
  fetchPassportActivity,
  fetchPassports,
  isUnauthorized,
  revokePassport,
} from './api'
import { STR } from './strings'
import type {
  PassportActivity,
  PassportSection,
  ShopPassport,
} from './types'

const SECTION_LABELS: Record<PassportSection, string> = STR.passport.sections

const SECTION_PHRASES: Record<PassportSection, string> = STR.passport.phrases

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

export default function Passport({
  onUnauthorized,
}: {
  /** Called when the session is rejected mid-use (expired cookie). */
  onUnauthorized: () => void
}): JSX.Element {
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
      ? STR.passport.shareNone
      : `${STR.passport.sharingStart} ${joinPhrases(sections.map((s) => SECTION_PHRASES[s]))} ` +
        `${STR.passport.sharingWith} ${lenderName || STR.passport.yourLender} ` +
        `${STR.passport.sharingFor} ${days} ${STR.passport.sharingDays} ` +
        `${STR.passport.sharingNoSales} ${STR.passport.sharingStop}`

  const loadList = useCallback(async () => {
    setListLoading(true)
    setListError(null)
    try {
      setPassports(await fetchPassports())
    } catch (err) {
      if (isUnauthorized(err)) {
        onUnauthorized()
        return
      }
      setListError(
        err instanceof Error ? err.message : 'Could not load passports.',
      )
    } finally {
      setListLoading(false)
    }
  }, [onUnauthorized])

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
      if (isUnauthorized(err)) {
        onUnauthorized()
        return
      }
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
      if (isUnauthorized(err)) {
        onUnauthorized()
        return
      }
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
      if (isUnauthorized(err)) {
        onUnauthorized()
        return
      }
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
    <div className="passport-layout">
      <section className="card passport-form" aria-label={STR.passport.shareTitle}>
        <h2>{STR.passport.shareTitle}</h2>
        <label className="field-label" htmlFor="lender-name">
          {STR.passport.lenderName}
        </label>
        <input
          id="lender-name"
          className="text-input"
          type="text"
          maxLength={60}
          placeholder={STR.passport.lenderPlaceholder}
          value={lender}
          onChange={(e) => setLender(e.target.value)}
          disabled={creating}
        />
        <fieldset className="check-group">
          <legend className="field-label">{STR.passport.whatToShare}</legend>
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
          <legend className="field-label">{STR.passport.linkLasts}</legend>
          <label className="check-row" key="days-7">
            <input
              type="radio"
              name="passport-days"
              checked={days === 7}
              onChange={() => setDays(7)}
              disabled={creating}
            />
            {STR.passport.days7}
          </label>
          <label className="check-row" key="days-30">
            <input
              type="radio"
              name="passport-days"
              checked={days === 30}
              onChange={() => setDays(30)}
              disabled={creating}
            />
            {STR.passport.days30}
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
            {creating ? STR.passport.creating : STR.passport.createButton}
          </button>
        </div>
      </section>

      {freshLink && (
        <section className="card link-card passport-link" aria-label={STR.passport.linkReadyTitle} aria-live="polite">
          <h2>{STR.passport.linkReadyTitle} {freshLink.lender}</h2>
          <p className="link-once">
            {STR.passport.linkOnce}
          </p>
          <p className="link-box">{freshLink.url}</p>
          <div className="row">
            <button
              type="button"
              className="btn secondary"
              onClick={() => void handleCopy()}
            >
              {copied ? STR.passport.copied : STR.passport.copy}
            </button>
            <a
              className="btn secondary"
              href={`https://wa.me/?text=${encodeURIComponent(
                `${STR.passport.whatsappHello} ${freshLink.lender}, ${STR.passport.whatsappIntro} ${freshLink.url}`,
              )}`}
              target="_blank"
              rel="noreferrer"
            >
              {STR.passport.whatsapp}
            </a>
          </div>
          <button
            type="button"
            className="btn linklike"
            onClick={() => setFreshLink(null)}
          >
            {STR.passport.doneHide}
          </button>
        </section>
      )}

      <section className="card passport-list" aria-label={STR.passport.sharedTitle}>
        <h2>{STR.passport.sharedTitle}</h2>
        {listLoading && (
          <div aria-label={STR.passport.linksLoading}>
            <p className="skeleton skeleton-text" aria-hidden="true">…</p>
            <p className="skeleton skeleton-text" aria-hidden="true">…</p>
          </div>
        )}
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
              {STR.common.retry}
            </button>
          </div>
        )}
        {!listLoading && !listError && sorted.length === 0 && (
          <div className="empty-state">
            <p>{STR.passport.noLinks}</p>
          </div>
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
                      {STR.passport.status[
                        p.status as keyof typeof STR.passport.status
                      ] ?? p.status}
                    </span>
                  </span>
                  <span className="passport-sub">
                    {p.scope.map((s) => SECTION_LABELS[s]).join(' · ')}
                  </span>
                  <span className="passport-sub">
                    {STR.passport.expires} {formatDate(p.expires_at)} · {STR.passport.viewed}{' '}
                    {p.view_count === 1 ? STR.passport.viewedOnce : `${p.view_count} ${STR.passport.viewedTimes}`}
                    {p.last_viewed_at
                      ? `, ${STR.passport.lastViewed} ${formatWhen(p.last_viewed_at)}`
                      : ''}
                  </span>
                </button>

                {expandedId === p.id && (
                  <div className="passport-detail">
                    {activityLoading && (
                      <p className="message">{STR.passport.viewsLoading}</p>
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
                          {STR.passport.noViews}
                        </p>
                      ) : (
                        <ul className="activity-list">
                          {activity.views.map((viewedAt, index) => (
                            <li key={`${viewedAt}-${index}`}>
                              {STR.passport.viewed} {formatWhen(viewedAt)}
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
                        {STR.passport.revokeTitle} {p.lender_name}? {STR.passport.revokeStops}
                      </p>
                      <div className="row">
                        <button
                          type="button"
                          className="btn primary"
                          onClick={() => void handleRevoke(p.id)}
                          disabled={revoking}
                        >
                          {revoking ? STR.passport.revoking : STR.passport.revokeYes}
                        </button>
                        <button
                          type="button"
                          className="btn secondary"
                          onClick={() => setConfirmRevokeId(null)}
                          disabled={revoking}
                        >
                          {STR.passport.keepSharing}
                        </button>
                      </div>
                    </div>
                  ) : (
                    <button
                      type="button"
                      className="btn secondary"
                      onClick={() => setConfirmRevokeId(p.id)}
                    >
                      {STR.passport.revoke}
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
