import { useCallback, useEffect, useRef, useState } from 'react'
import {
  confirmEntry,
  fetchLedger,
  fetchMe,
  isUnauthorized,
  logout,
  postEntry,
  postTranscription,
  type Session,
} from './api'
import AuthScreen, { type AuthMode } from './Auth'
import Credit from './Credit'
import { formatKES } from './format'
import LenderPassport from './LenderPassport'
import Passport from './Passport'
import { STR } from './strings'
import type { Entry, EntryType } from './types'
import { VOICE_MAX_SECONDS, VoiceRecorder } from './voice'
import './styles.css'

/** Lender links live at /p/<token>: no tab bar, no shopkeeper UI. */
function lenderTokenFromPath(): string | null {
  const match = window.location.pathname.match(/^\/p\/([^/]+)\/?$/)
  return match ? decodeURIComponent(match[1]) : null
}

const TYPE_LABELS: Record<EntryType, string> = STR.entries.types

function formatQuantity(quantity: number): string {
  return Number.isInteger(quantity) ? String(quantity) : String(quantity)
}

/** Plain-language read-back of what Spark understood. */
function readBackSentence(
  type: EntryType,
  item: string,
  quantity: number,
  unit: string,
  amountKes: number,
  counterparty: string | null,
): string {
  const what = `${formatQuantity(quantity)} ${unit} ${item}`.trim()
  const money = formatKES(amountKes)
  switch (type) {
    case 'sale':
      return `Sale: ${what} for ${money}.`
    case 'purchase':
      return `Purchase for the shop: ${what} for ${money}.`
    case 'expense':
      return `Expense: ${what} for ${money}.`
    case 'credit_given':
      return counterparty
        ? `Credit given to ${counterparty}: ${what} for ${money} (to be paid later).`
        : `Credit given: ${what} for ${money} (to be paid later).`
    case 'credit_received':
      return counterparty
        ? `Credit received from ${counterparty}: ${money} for ${what}.`
        : `Credit received: ${money} for ${what}.`
  }
}

function isToday(iso: string): boolean {
  const d = new Date(iso)
  const now = new Date()
  return (
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  )
}

function formatVoiceTime(totalSeconds: number): string {
  const capped = Math.min(totalSeconds, VOICE_MAX_SECONDS)
  const minutes = Math.floor(capped / 60)
  const seconds = capped % 60
  return `${minutes}:${String(seconds).padStart(2, '0')}`
}

function formatWhen(iso: string): string {
  return new Date(iso).toLocaleString('en-KE', {
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** Group ledger rows under "Today", "Yesterday", or a plain date. */
function dayLabel(iso: string): string {
  const d = new Date(iso)
  const now = new Date()
  const startOf = (x: Date): number =>
    new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const diffDays = Math.round(
    (startOf(now) - startOf(d)) / (24 * 60 * 60 * 1000),
  )
  if (diffDays <= 0) return 'Today'
  if (diffDays === 1) return 'Yesterday'
  return d.toLocaleDateString('en-KE', {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
  })
}

function dayKey(iso: string): string {
  const d = new Date(iso)
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`
}

/** True while the browser thinks it is online; false shows the banner. */
function useOnline(): boolean {
  const [online, setOnline] = useState(
    () => (typeof navigator === 'undefined' ? true : navigator.onLine),
  )
  useEffect(() => {
    const goOnline = (): void => setOnline(true)
    const goOffline = (): void => setOnline(false)
    window.addEventListener('online', goOnline)
    window.addEventListener('offline', goOffline)
    return () => {
      window.removeEventListener('online', goOnline)
      window.removeEventListener('offline', goOffline)
    }
  }, [])
  return online
}

/** Ledger rows grouped under Today / Yesterday / date headings. */
function LedgerGroups({ ledger }: { ledger: Entry[] }): JSX.Element {
  const groups = new Map<string, Entry[]>()
  for (const entry of ledger) {
    const key = dayKey(entry.occurred_at)
    const list = groups.get(key)
    if (list) list.push(entry)
    else groups.set(key, [entry])
  }
  return (
    <>
      {[...groups.values()].map((rows) => (
        <div className="day-group" key={dayKey(rows[0].occurred_at)}>
          <h3>{dayLabel(rows[0].occurred_at)}</h3>
          <ul className="ledger-list">
            {rows.map((entry) => (
              <li key={entry.id} className="ledger-row">
                <div className="ledger-left">
                  <span className={`badge badge-${entry.type}`}>
                    {TYPE_LABELS[entry.type]}
                  </span>
                  <div className="ledger-details">
                    <span className="ledger-item">
                      {formatQuantity(entry.quantity)} {entry.unit} {entry.item}
                    </span>
                    <span className="ledger-meta">{formatWhen(entry.occurred_at)}</span>
                  </div>
                </div>
                <strong className="ledger-amount">{formatKES(entry.amount_kes)}</strong>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </>
  )
}

function MicIcon(): JSX.Element {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">
      <rect x="7" y="1.5" width="6" height="10" rx="3" fill="currentColor" />
      <path
        d="M4 9.5a6 6 0 0 0 12 0"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
      <line
        x1="10"
        y1="15.5"
        x2="10"
        y2="18.5"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  )
}

function TabIcon({ kind }: { kind: 'entries' | 'credit' | 'passport' }): JSX.Element {
  if (kind === 'entries') {
    return (
      <svg className="tab-icon" viewBox="0 0 20 20" aria-hidden="true">
        <path
          d="M4 5.5h12M4 10h12M4 14.5h7"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
      </svg>
    )
  }
  if (kind === 'credit') {
    return (
      <svg className="tab-icon" viewBox="0 0 20 20" aria-hidden="true">
        <path
          d="M3.5 14.5a7 7 0 0 1 13 0"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
        <path
          d="M10 14.5l3.5-4.5"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
        />
        <circle cx="10" cy="14.5" r="1.2" fill="currentColor" />
      </svg>
    )
  }
  return (
    <svg className="tab-icon" viewBox="0 0 20 20" aria-hidden="true">
      <rect
        x="3"
        y="5.5"
        width="14"
        height="9"
        rx="2"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
      />
      <circle cx="7.5" cy="10" r="1.6" fill="currentColor" />
      <path
        d="M11.5 8.5H16M11.5 11.5H16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
      />
    </svg>
  )
}

export default function App(): JSX.Element {
  const online = useOnline()
  const [tab, setTab] = useState<'entries' | 'credit' | 'passport'>('entries')
  // Theme lives only in memory: the OS preference chooses the first value,
  // and the header toggle flips it for this page load. Nothing is stored.
  const [theme, setTheme] = useState<'light' | 'dark'>(() =>
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-color-scheme: dark)').matches
      ? 'dark'
      : 'light',
  )
  const lenderToken = lenderTokenFromPath()
  // Session profile only (shop name for the header): the session token
  // itself lives in the HttpOnly cookie and never touches JS state.
  const [me, setMe] = useState<Session | null>(null)
  const [authChecked, setAuthChecked] = useState(false)
  const [authNotice, setAuthNotice] = useState<string | null>(null)
  const [authMode, setAuthMode] = useState<AuthMode>('login')
  useEffect(() => {
    let live = true
    fetchMe()
      .then((session) => {
        if (live) setMe(session)
      })
      .catch(() => {
        if (live) setMe(null)
      })
      .finally(() => {
        if (live) setAuthChecked(true)
      })
    return () => {
      live = false
    }
  }, [])
  if (lenderToken) {
    return (
      <main className="app app-lender" data-theme={theme}>
        <LenderPassport token={lenderToken} />
      </main>
    )
  }
  const [text, setText] = useState('')
  const [entryLoading, setEntryLoading] = useState(false)
  const [entryError, setEntryError] = useState<string | null>(null)
  const entryBoxRef = useRef<HTMLTextAreaElement | null>(null)

  // Voice entry: idle -> recording -> working (transcribing) -> idle.
  // The transcript lands in the textarea for review; nothing auto-saves.
  const [voice, setVoice] = useState<'idle' | 'recording' | 'working'>('idle')
  const [voiceSecs, setVoiceSecs] = useState(0)
  const [voiceError, setVoiceError] = useState<string | null>(null)
  const recorderRef = useRef<VoiceRecorder | null>(null)
  const timerRef = useRef<number | null>(null)

  function stopVoiceTimer(): void {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current)
      timerRef.current = null
    }
  }

  // Release the mic if the screen unmounts mid-recording.
  useEffect(() => {
    return () => {
      recorderRef.current?.cancel()
      recorderRef.current = null
      if (timerRef.current !== null) {
        window.clearInterval(timerRef.current)
      }
    }
  }, [])

  /** Drop every in-memory shop detail and show the login screen. */
  function clearShopData(notice: string | null): void {
    recorderRef.current?.cancel()
    recorderRef.current = null
    stopVoiceTimer()
    setText('')
    setDraft(null)
    setEditing(false)
    clearEditErrors()
    setConfirmError(null)
    setConfirmLoading(false)
    setEntryLoading(false)
    setEntryError(null)
    setVoice('idle')
    setVoiceSecs(0)
    setVoiceError(null)
    setLedger([])
    setLedgerError(null)
    setLedgerLoading(false)
    setMe(null)
    setAuthNotice(notice)
    setTab('credit')
  }

  function handleUnauthorized(): void {
    clearShopData(STR.app.sessionExpired)
  }

  function handleLoggedIn(session: Session): void {
    setMe(session)
    setAuthNotice(null)
    setTab('entries')
  }

  async function handleLogout(): Promise<void> {
    try {
      await logout()
    } catch {
      // Server revocation is best-effort; local state clears regardless.
    }
    clearShopData(null)
  }

  async function stopVoice(): Promise<void> {
    const recorder = recorderRef.current
    recorderRef.current = null
    stopVoiceTimer()
    if (!recorder) {
      setVoice('idle')
      return
    }
    setVoice('working')
    setVoiceError(null)
    try {
      const clip = await recorder.stop()
      setText(await postTranscription(clip.blob))
      entryBoxRef.current?.focus()
    } catch (err) {
      if (isUnauthorized(err)) {
        handleUnauthorized()
        return
      }
      setVoiceError(err instanceof Error ? err.message : 'Voice input failed. Try again.')
    } finally {
      setVoice('idle')
      setVoiceSecs(0)
    }
  }

  async function handleMic(): Promise<void> {
    if (voice === 'recording') {
      await stopVoice()
      return
    }
    if (voice !== 'idle') return
    const recorder = new VoiceRecorder(VOICE_MAX_SECONDS)
    recorderRef.current = recorder
    setVoiceError(null)
    try {
      await recorder.start()
    } catch (err) {
      recorderRef.current = null
      setVoiceError(err instanceof Error ? err.message : 'Could not start the microphone.')
      return
    }
    const startedAt = Date.now()
    setVoiceSecs(0)
    setVoice('recording')
    timerRef.current = window.setInterval(() => {
      setVoiceSecs(Math.floor((Date.now() - startedAt) / 1000))
    }, 250)
  }

  // Draft from POST /api/entry. Nothing reaches the ledger until confirm.
  const [draft, setDraft] = useState<Entry | null>(null)

  const [editing, setEditing] = useState(false)
  const [editItem, setEditItem] = useState('')
  const [editQuantity, setEditQuantity] = useState('')
  const [editAmount, setEditAmount] = useState('')
  const [editItemError, setEditItemError] = useState<string | null>(null)
  const [editQuantityError, setEditQuantityError] = useState<string | null>(null)
  const [editAmountError, setEditAmountError] = useState<string | null>(null)

  function clearEditErrors(): void {
    setEditItemError(null)
    setEditQuantityError(null)
    setEditAmountError(null)
  }

  const [confirmLoading, setConfirmLoading] = useState(false)
  const [confirmError, setConfirmError] = useState<string | null>(null)

  const [ledger, setLedger] = useState<Entry[]>([])
  const [ledgerLoading, setLedgerLoading] = useState(true)
  const [ledgerError, setLedgerError] = useState<string | null>(null)

  const loadLedger = useCallback(async () => {
    setLedgerLoading(true)
    setLedgerError(null)
    try {
      setLedger(await fetchLedger())
    } catch (err) {
      if (isUnauthorized(err)) {
        handleUnauthorized()
        return
      }
      setLedgerError(
        err instanceof Error ? err.message : 'Could not load the ledger.',
      )
    } finally {
      setLedgerLoading(false)
    }
  }, [])

  useEffect(() => {
    // Logged-in only: anonymous visitors have no ledger, and firing this
    // while logged out would mistake "never logged in" for expiry.
    if (me) {
      void loadLedger()
    }
  }, [loadLedger, me])

  async function handleAdd(): Promise<void> {
    if (!text.trim()) {
      setEntryError(STR.entries.typeFirst)
      return
    }
    setEntryLoading(true)
    setEntryError(null)
    try {
      const entry = await postEntry(text.trim())
      setDraft(entry)
      setEditing(false)
      clearEditErrors()
      setConfirmError(null)
    } catch (err) {
      if (isUnauthorized(err)) {
        handleUnauthorized()
        return
      }
      setEntryError(err instanceof Error ? err.message : 'Please try again.')
    } finally {
      setEntryLoading(false)
    }
  }

  function startEdit(): void {
    if (!draft) return
    setEditItem(draft.item)
    setEditQuantity(String(draft.quantity))
    setEditAmount(String(Math.round(draft.amount_kes)))
    clearEditErrors()
    setEditing(true)
  }

  function handleCancel(): void {
    // The draft stays unconfirmed in the backend, so it never appears
    // in the ledger. We just drop it from this screen.
    setDraft(null)
    setEditing(false)
    clearEditErrors()
    setConfirmError(null)
  }

  async function handleConfirm(): Promise<void> {
    if (!draft) return
    let item = draft.item
    let quantity = draft.quantity
    let amountKes = Math.round(draft.amount_kes)
    if (editing) {
      // Validate before sending: no request goes out while a field is invalid.
      let valid = true
      item = editItem.trim()
      if (!item) {
        setEditItemError(STR.entries.itemRequired)
        valid = false
      } else {
        setEditItemError(null)
      }
      const quantityRaw = editQuantity.trim()
      quantity = Number(quantityRaw)
      if (
        quantityRaw === '' ||
        !Number.isFinite(quantity) ||
        quantity <= 0
      ) {
        setEditQuantityError(STR.entries.quantityInvalid)
        valid = false
      } else {
        setEditQuantityError(null)
      }
      const amountRaw = editAmount.trim()
      if (!/^\d+$/.test(amountRaw)) {
        setEditAmountError(STR.entries.amountInvalid)
        valid = false
      } else {
        amountKes = Number(amountRaw)
        if (amountKes < 1) {
          setEditAmountError(STR.entries.amountInvalid)
          valid = false
        } else {
          setEditAmountError(null)
        }
      }
      if (!valid) {
        return
      }
    }
    setConfirmLoading(true)
    setConfirmError(null)
    try {
      await confirmEntry(draft.id, { item, quantity, amount_kes: amountKes })
      setDraft(null)
      setEditing(false)
      setText('')
      await loadLedger()
    } catch (err) {
      if (isUnauthorized(err)) {
        handleUnauthorized()
        return
      }
      setConfirmError(
        err instanceof Error ? err.message : 'Could not confirm. Try again.',
      )
    } finally {
      setConfirmLoading(false)
    }
  }

  const shownItem = editing ? editItem || '(no item)' : (draft?.item ?? '')
  const shownQuantity = editing ? Number(editQuantity) || 0 : (draft?.quantity ?? 0)
  const shownAmount = editing
    ? Math.round(Number(editAmount) || 0)
    : Math.round(draft?.amount_kes ?? 0)

  const todaySales = ledger
    .filter((e) => e.type === 'sale' && isToday(e.occurred_at))
    .reduce((sum, e) => sum + Math.round(e.amount_kes), 0)
  const todaySpent = ledger
    .filter(
      (e) => (e.type === 'expense' || e.type === 'purchase') && isToday(e.occurred_at),
    )
    .reduce((sum, e) => sum + Math.round(e.amount_kes), 0)

  if (!authChecked) {
    return (
      <main className="app" data-theme={theme}>
        <header className="app-header">
          <div className="topbar">
            <h1 className="brand">{STR.app.name}</h1>
            <div className="account">
              <button
                type="button"
                className="theme-toggle"
                onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
                aria-pressed={theme === 'dark'}
              >
                {theme === 'dark' ? STR.app.themeLight : STR.app.themeDark}
              </button>
            </div>
          </div>
          <p className="tagline">{STR.app.tagline}</p>
        </header>
        <p className="message">{STR.common.loading}</p>
      </main>
    )
  }

  return (
    <main className="app" data-theme={theme}>
      <header className="app-header">
        <div className="topbar">
          <h1 className="brand">{STR.app.name}</h1>
          <div className="account">
            <button
              type="button"
              className="theme-toggle"
              onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
              aria-pressed={theme === 'dark'}
            >
              {theme === 'dark' ? STR.app.themeLight : STR.app.themeDark}
            </button>
            {me ? (
              <>
                <span className="shop-name">
                  {STR.app.loggedInAs}: {me.shop_name}
                </span>
                <button
                  type="button"
                  className="btn linklike"
                  onClick={() => void handleLogout()}
                >
                  {STR.app.logout}
                </button>
              </>
            ) : (
              <button
                type="button"
                className="btn linklike"
                onClick={() => {
                  setAuthMode('login')
                  setTab('entries')
                }}
              >
                {STR.app.login}
              </button>
            )}
          </div>
        </div>
        <p className="tagline">{STR.app.tagline}</p>
      </header>

      {!online && (
        <p className="offline-banner" role="status">
          {STR.app.offline}
        </p>
      )}

      <nav className="tabs" aria-label="Sections" role="tablist">
        <button
          type="button"
          role="tab"
          className={tab === 'entries' ? 'tab active' : 'tab'}
          aria-selected={tab === 'entries'}
          onClick={() => setTab('entries')}
        >
          <TabIcon kind="entries" />
          <span>{STR.app.tabs.entries}</span>
        </button>
        <button
          type="button"
          role="tab"
          className={tab === 'credit' ? 'tab active' : 'tab'}
          aria-selected={tab === 'credit'}
          onClick={() => setTab('credit')}
        >
          <TabIcon kind="credit" />
          <span>{STR.app.tabs.credit}</span>
        </button>
        <button
          type="button"
          role="tab"
          className={tab === 'passport' ? 'tab active' : 'tab'}
          aria-selected={tab === 'passport'}
          onClick={() => setTab('passport')}
        >
          <TabIcon kind="passport" />
          <span>{STR.app.tabs.passport}</span>
        </button>
      </nav>

      {tab === 'entries' ? (
        me ? (
        <div className="entries-layout">
      <div className="entries-columns">
        <div className="entries-primary">
      <section className="card" aria-label="New entry">
        <div className="mic-hero">
          <button
            type="button"
            className={voice === 'recording' ? 'mic-btn recording' : 'mic-btn'}
            onClick={() => void handleMic()}
            disabled={voice === 'working' || entryLoading}
            aria-label={
              voice === 'recording' ? STR.entries.micStop : STR.entries.micHeroLabel
            }
          >
            <MicIcon />
          </button>
          {voice === 'recording' ? (
            <>
              <p className="mic-timer" role="timer" aria-label={STR.entries.micListening}>
                {formatVoiceTime(voiceSecs)}
              </p>
              <button
                type="button"
                className="btn secondary block mic-stop"
                onClick={() => void stopVoice()}
                disabled={entryLoading}
              >
                {STR.entries.micStop}
              </button>
            </>
          ) : (
            <p className="mic-caption">
              {voice === 'working' ? STR.entries.voiceWorking : STR.entries.micHeroLabel}
            </p>
          )}
        </div>
        <div className="divider" aria-hidden="true">
          <span>{STR.entries.typeDivider}</span>
        </div>
        <label className="field-label" htmlFor="entry-text">
          {STR.entries.typeLabel}
        </label>
        <textarea
          id="entry-text"
          ref={entryBoxRef}
          className="entry-box"
          rows={3}
          placeholder={STR.entries.typePlaceholder}
          value={text}
          onChange={(e) => setText(e.target.value)}
          disabled={entryLoading}
        />
        {entryError && (
          <p className="message error" role="alert">
            {entryError}
          </p>
        )}
        <div className="row">
          <button
            type="button"
            className="btn primary"
            onClick={() => void handleAdd()}
            disabled={entryLoading || !text.trim()}
          >
            {entryLoading ? STR.entries.checking : STR.entries.add}
          </button>
        </div>
        {voiceError && (
          <div>
            <p className="message error" role="alert">
              {voiceError}
            </p>
            <button type="button" className="btn secondary" onClick={() => void handleMic()}>
              {STR.common.retry}
            </button>
          </div>
        )}
      </section>

      {draft && (
        <section className="card draft" aria-label={STR.entries.confirmTitle} aria-live="polite">
          <h2>{STR.entries.confirmTitle}</h2>
          <p className="readback">
            {readBackSentence(
              draft.type,
              shownItem,
              shownQuantity,
              draft.unit,
              shownAmount,
              draft.counterparty,
            )}
          </p>

          {editing ? (
            <div className="edit-form">
              <label className="field-label" htmlFor="edit-item">
                {STR.entries.item}
              </label>
              <input
                id="edit-item"
                className="text-input"
                type="text"
                value={editItem}
                onChange={(e) => {
                  setEditItem(e.target.value)
                  setEditItemError(null)
                }}
              />
              {editItemError && (
                <p className="field-error" role="alert">
                  {editItemError}
                </p>
              )}
              <div className="row">
                <div className="field">
                  <label className="field-label" htmlFor="edit-quantity">
                    {STR.entries.quantity} ({draft.unit})
                  </label>
                  <input
                    id="edit-quantity"
                    className="text-input"
                    type="number"
                    min="0"
                    step="any"
                    inputMode="decimal"
                    value={editQuantity}
                    onChange={(e) => {
                      setEditQuantity(e.target.value)
                      setEditQuantityError(null)
                    }}
                  />
                  {editQuantityError && (
                    <p className="field-error" role="alert">
                      {editQuantityError}
                    </p>
                  )}
                </div>
                <div className="field">
                  <label className="field-label" htmlFor="edit-amount">
                    {STR.entries.amount}
                  </label>
                  <input
                    id="edit-amount"
                    className="text-input"
                    type="number"
                    min="0"
                    step="1"
                    inputMode="numeric"
                    value={editAmount}
                    onChange={(e) => {
                      setEditAmount(e.target.value)
                      setEditAmountError(null)
                    }}
                  />
                  {editAmountError && (
                    <p className="field-error" role="alert">
                      {editAmountError}
                    </p>
                  )}
                </div>
              </div>
            </div>
          ) : (
            <div className="row">
              <button type="button" className="btn secondary block" onClick={startEdit}>
                {STR.common.edit}
              </button>
            </div>
          )}

          {confirmError && (
            <p className="message error" role="alert">
              {confirmError}
            </p>
          )}
          <div className="row">
            <button
              type="button"
              className="btn primary"
              onClick={() => void handleConfirm()}
              disabled={confirmLoading}
            >
              {confirmLoading ? STR.common.saving : STR.common.confirm}
            </button>
            {!editing && (
              <button
                type="button"
                className="btn secondary"
                onClick={handleCancel}
                disabled={confirmLoading}
              >
                {STR.common.cancelShort}
              </button>
            )}
          </div>
          {editing && (
            <button
              type="button"
              className="btn linklike"
              onClick={handleCancel}
              disabled={confirmLoading}
            >
              {STR.common.cancel}
            </button>
          )}
          <p className="hint">{STR.entries.confirmHint}</p>
        </section>
      )}
        </div>
      <section className="card entries-totals" aria-label={STR.entries.todayTotals}>
        <div className="totals">
          <div className="total">
            <span className="total-label">{STR.entries.todaySales}</span>
            <strong>{formatKES(todaySales)}</strong>
          </div>
          <div className="total">
            <span className="total-label">{STR.entries.todaySpent}</span>
            <strong>{formatKES(todaySpent)}</strong>
          </div>
        </div>
      </section>
        <div className="entries-ledger">
      <section className="card" aria-label={STR.entries.ledgerTitle}>
        <h2>{STR.entries.ledgerTitle}</h2>

        <div className="ledger-scroll">
          {ledgerLoading && (
            <div aria-label={STR.entries.ledgerLoading}>
              <p className="skeleton skeleton-big" aria-hidden="true">…</p>
              <p className="skeleton skeleton-text" aria-hidden="true">…</p>
              <p className="skeleton skeleton-text" aria-hidden="true">…</p>
            </div>
          )}
          {!ledgerLoading && ledgerError && (
            <div>
              <p className="message error" role="alert">
                {ledgerError}
              </p>
              <button type="button" className="btn secondary" onClick={() => void loadLedger()}>
                {STR.common.retry}
              </button>
            </div>
          )}
          {!ledgerLoading && !ledgerError && ledger.length === 0 && (
            <div className="empty-state">
              <p>{STR.entries.ledgerEmpty}</p>
            </div>
          )}
          {!ledgerLoading && !ledgerError && ledger.length > 0 && (
            <LedgerGroups ledger={ledger} />
          )}
        </div>
      </section>
        </div>
      </div>
        </div>
        ) : (
          <AuthScreen
            notice={authNotice}
            mode={authMode}
            onModeChange={setAuthMode}
            onDone={handleLoggedIn}
          />
        )
      ) : tab === 'credit' ? (
        <Credit
          key={me ? `shop-${me.shop_id}` : 'demo'}
          demo={!me}
          onAuthRequest={(next) => {
            setAuthMode(next)
            setTab('entries')
          }}
          onUnauthorized={handleUnauthorized}
        />
      ) : me ? (
        <Passport key={`shop-${me.shop_id}`} onUnauthorized={handleUnauthorized} />
      ) : (
        <AuthScreen
          notice={authNotice}
          mode={authMode}
          onModeChange={setAuthMode}
          onDone={handleLoggedIn}
        />
      )}
    </main>
  )
}
