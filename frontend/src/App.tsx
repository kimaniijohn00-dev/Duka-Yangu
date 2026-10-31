import { useCallback, useEffect, useRef, useState } from 'react'
import { confirmEntry, fetchLedger, postEntry, postTranscription } from './api'
import Credit from './Credit'
import { formatKES } from './format'
import LenderPassport from './LenderPassport'
import Passport from './Passport'
import type { Entry, EntryType } from './types'
import { VOICE_MAX_SECONDS, VoiceRecorder } from './voice'
import './styles.css'

/** Lender links live at /p/<token>: no tab bar, no shopkeeper UI. */
function lenderTokenFromPath(): string | null {
  const match = window.location.pathname.match(/^\/p\/([^/]+)\/?$/)
  return match ? decodeURIComponent(match[1]) : null
}

const TYPE_LABELS: Record<EntryType, string> = {
  sale: 'Sale',
  purchase: 'Purchase',
  expense: 'Expense',
  credit_given: 'Credit given',
  credit_received: 'Credit received',
}

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

export default function App(): JSX.Element {
  const [tab, setTab] = useState<'entries' | 'credit' | 'passport'>('entries')
  const lenderToken = lenderTokenFromPath()
  if (lenderToken) {
    return (
      <main className="app">
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
      setLedgerError(
        err instanceof Error ? err.message : 'Could not load the ledger.',
      )
    } finally {
      setLedgerLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadLedger()
  }, [loadLedger])

  async function handleAdd(): Promise<void> {
    if (!text.trim()) {
      setEntryError('Type what happened first, e.g. “sold 5kg sugar for 800 bob”.')
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
        setEditItemError('Give the item a name — e.g. sugar, bread, airtime.')
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
        setEditQuantityError('Quantity must be a number greater than 0.')
        valid = false
      } else {
        setEditQuantityError(null)
      }
      const amountRaw = editAmount.trim()
      if (!/^\d+$/.test(amountRaw)) {
        setEditAmountError('Amount must be a whole number, like 800.')
        valid = false
      } else {
        amountKes = Number(amountRaw)
        if (amountKes < 1) {
          setEditAmountError('Amount must be greater than 0.')
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

  return (
    <main className="app">
      <header className="app-header">
        <h1>Duka-Yangu</h1>
        <p className="tagline">Type what happened. Confirm. Done.</p>
      </header>

      {tab === 'entries' ? (
        <>
      <section className="card" aria-label="New entry">
        <label className="field-label" htmlFor="entry-text">
          New entry
        </label>
        <textarea
          id="entry-text"
          ref={entryBoxRef}
          className="entry-box"
          rows={3}
          placeholder="e.g. sold 5kg sugar for 800 bob"
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
            {entryLoading ? 'Reading…' : 'Add'}
          </button>
          <button
            type="button"
            className={voice === 'recording' ? 'btn mic recording' : 'btn mic'}
            onClick={() => void handleMic()}
            disabled={voice === 'working' || entryLoading}
            title={
              voice === 'recording'
                ? 'Stop recording'
                : 'Record a voice entry'
            }
          >
            <MicIcon />
            {voice === 'recording'
              ? `Stop (${formatVoiceTime(voiceSecs)})`
              : voice === 'working'
                ? 'Listening…'
                : 'Voice'}
          </button>
        </div>
        {voice === 'recording' && (
          <p className="voice-status" role="status">
            <span className="rec-dot" aria-hidden="true" />
            Recording… {formatVoiceTime(voiceSecs)} — tap Stop when done
            (max {formatVoiceTime(VOICE_MAX_SECONDS)}).
          </p>
        )}
        {voice === 'working' && (
          <p className="voice-status" role="status">
            Turning speech into text… check the words, then press Add.
          </p>
        )}
        {voiceError && (
          <p className="message error" role="alert">
            {voiceError}
          </p>
        )}
      </section>

      {draft && (
        <section className="card draft" aria-label="Check before saving" aria-live="polite">
          <h2>Check before saving</h2>
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
                Item
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
                    Quantity ({draft.unit})
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
                    Amount (KES)
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
            <button type="button" className="btn secondary" onClick={startEdit}>
              Edit
            </button>
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
              {confirmLoading ? 'Saving…' : 'Confirm'}
            </button>
            {!editing && (
              <button
                type="button"
                className="btn secondary"
                onClick={handleCancel}
                disabled={confirmLoading}
              >
                Cancel
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
              Cancel entry
            </button>
          )}
          <p className="hint">Nothing is saved to the ledger until you tap Confirm.</p>
        </section>
      )}

      <section className="card" aria-label="Ledger">
        <h2>Ledger</h2>
        <div className="totals">
          <div className="total">
            <span className="total-label">Sales today</span>
            <strong>{formatKES(todaySales)}</strong>
          </div>
          <div className="total">
            <span className="total-label">Spent today</span>
            <strong>{formatKES(todaySpent)}</strong>
          </div>
        </div>

        {ledgerLoading && <p className="message">Loading ledger…</p>}
        {!ledgerLoading && ledgerError && (
          <div>
            <p className="message error" role="alert">
              {ledgerError}
            </p>
            <button type="button" className="btn secondary" onClick={() => void loadLedger()}>
              Retry
            </button>
          </div>
        )}
        {!ledgerLoading && !ledgerError && ledger.length === 0 && (
          <p className="message">Ledger is empty — add your first sale above.</p>
        )}
        {!ledgerLoading && !ledgerError && ledger.length > 0 && (
          <ul className="ledger-list">
            {ledger.map((entry) => (
              <li key={entry.id} className="ledger-row">
                <div className="ledger-main">
                  <span className={`badge badge-${entry.type}`}>
                    {TYPE_LABELS[entry.type]}
                  </span>
                  <span className="ledger-item">
                    {formatQuantity(entry.quantity)} {entry.unit} {entry.item}
                  </span>
                </div>
                <div className="ledger-sub">
                  <strong>{formatKES(entry.amount_kes)}</strong>
                  <span className="ledger-when">{formatWhen(entry.occurred_at)}</span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
        </>
      ) : tab === 'credit' ? (
        <Credit />
      ) : (
        <Passport />
      )}

      <nav className="tabs" aria-label="Sections">
        <button
          type="button"
          className={tab === 'entries' ? 'tab active' : 'tab'}
          aria-selected={tab === 'entries'}
          onClick={() => setTab('entries')}
        >
          Entries
        </button>
        <button
          type="button"
          className={tab === 'credit' ? 'tab active' : 'tab'}
          aria-selected={tab === 'credit'}
          onClick={() => setTab('credit')}
        >
          Credit
        </button>
        <button
          type="button"
          className={tab === 'passport' ? 'tab active' : 'tab'}
          aria-selected={tab === 'passport'}
          onClick={() => setTab('passport')}
        >
          Passport
        </button>
      </nav>
    </main>
  )
}
