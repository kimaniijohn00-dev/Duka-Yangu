import { useRef, useState } from 'react'
import { ApiStatusError, login, register, type Session } from './api'
import { SUPPORT_CONTACT } from './config'
import { STR } from './strings'

function digitsOnly(value: string): string {
  return value.replace(/\D/g, '')
}

export type AuthMode = 'login' | 'register'

interface Props {
  /** One-off notice, e.g. after a session expires. Shown once at the top. */
  notice: string | null
  mode: AuthMode
  onModeChange: (mode: AuthMode) => void
  onDone: (session: Session) => void
}

export default function AuthScreen({ notice, mode, onModeChange, onDone }: Props): JSX.Element {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [failCount, setFailCount] = useState(0)
  const [showPin, setShowPin] = useState(false)
  const [showConfirm, setShowConfirm] = useState(false)
  // Uncontrolled inputs: values are read once at submit and never kept
  // in state, so no PIN ever sits in a JS variable, storage, or log.
  // The toggles above only flip visibility, never the value.
  const phoneRef = useRef<HTMLInputElement>(null)
  const pinRef = useRef<HTMLInputElement>(null)
  const confirmRef = useRef<HTMLInputElement>(null)
  const shopRef = useRef<HTMLInputElement>(null)
  const inviteRef = useRef<HTMLInputElement>(null)

  function switchMode(next: 'login' | 'register'): void {
    onModeChange(next)
    setError(null)
    setFailCount(0)
    setShowPin(false)
    setShowConfirm(false)
  }

  function loginMessage(err: unknown): { text: string; failed: boolean } {
    if (err instanceof ApiStatusError) {
      if (err.status === 401 || err.status === 403) {
        return { text: STR.auth.loginFailed, failed: true }
      }
      if (err.status === 429) {
        return { text: STR.auth.lockedOut, failed: true }
      }
      return { text: STR.auth.loginGeneric, failed: false }
    }
    return { text: STR.auth.serverUnreachable, failed: false }
  }

  async function handleLogin(): Promise<void> {
    const phone = phoneRef.current?.value.trim() ?? ''
    const pin = pinRef.current?.value ?? ''
    if (!phone) {
      setError(STR.auth.phoneRequired)
      return
    }
    if (!/^\d{6,12}$/.test(pin)) {
      setError(STR.auth.pinRequired)
      return
    }
    setBusy(true)
    setError(null)
    try {
      const session = await login({ phone, pin })
      setFailCount(0)
      onDone(session)
    } catch (err) {
      const mapped = loginMessage(err)
      if (mapped.failed) {
        const next = failCount + 1
        setFailCount(next)
        setError(
          next >= 3 ? `${mapped.text} ${STR.auth.repeatedHint}` : mapped.text,
        )
      } else {
        setError(mapped.text)
      }
    } finally {
      setBusy(false)
    }
  }

  function registerMessage(err: unknown): string {
    if (err instanceof ApiStatusError) {
      if (err.status === 429) {
        return STR.auth.lockedOut
      }
      if (
        err.status === 422 ||
        err.status === 409 ||
        err.status === 403
      ) {
        return err.message
      }
      return STR.auth.registerGeneric
    }
    return STR.auth.serverUnreachable
  }

  async function handleRegister(): Promise<void> {
    const phone = phoneRef.current?.value.trim() ?? ''
    const pin = pinRef.current?.value ?? ''
    const confirmPin = confirmRef.current?.value ?? ''
    const shop = shopRef.current?.value.trim() ?? ''
    const invite = inviteRef.current?.value.trim() ?? ''
    if (!phone) {
      setError(STR.auth.phoneRequired)
      return
    }
    if (!/^\d{6,12}$/.test(pin)) {
      setError(STR.auth.pinRequired)
      return
    }
    if (pin !== confirmPin) {
      setError(STR.auth.pinsNoMatch)
      return
    }
    if (!shop) {
      setError(STR.auth.shopRequired)
      return
    }
    if (!invite) {
      setError(STR.auth.inviteRequired)
      return
    }
    setBusy(true)
    setError(null)
    try {
      const session = await register({
        phone,
        pin,
        shop_name: shop,
        invite_code: invite,
      })
      onDone(session)
    } catch (err) {
      setError(registerMessage(err))
    } finally {
      setBusy(false)
    }
  }

  function sanitizePin(box: HTMLInputElement | null): void {
    if (box) {
      const clean = digitsOnly(box.value).slice(0, 12)
      if (box.value !== clean) {
        box.value = clean
      }
    }
  }

  return (
    <div>
      {notice && (
        <p className="message" role="status">
          {notice}
        </p>
      )}
      <section className="card" aria-label={mode === 'login' ? STR.auth.loginTitle : STR.auth.joinTitle}>
        <h2>{mode === 'login' ? STR.auth.loginTitle : STR.auth.joinTitle}</h2>
        <p className="hint">
          {mode === 'login' ? STR.auth.loginBody : STR.auth.registerBody}
        </p>
        <label className="field-label" htmlFor="auth-phone">
          {STR.auth.phone}
        </label>
        <input
          id="auth-phone"
          ref={phoneRef}
          className="text-input"
          type="tel"
          autoComplete="tel"
          inputMode="tel"
          placeholder={STR.auth.phonePlaceholder}
          disabled={busy}
        />
        <label className="field-label" htmlFor="auth-pin">
          {STR.auth.pin}
        </label>
        <div className="pin-wrap">
          <input
            id="auth-pin"
            ref={pinRef}
            className="text-input"
            type={showPin ? 'text' : 'password'}
            inputMode="numeric"
            autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            placeholder={STR.auth.pinPlaceholder}
            maxLength={12}
            onChange={(e) => sanitizePin(e.target)}
            disabled={busy}
          />
          <button
            type="button"
            className="pin-toggle"
            onClick={() => setShowPin((v) => !v)}
            aria-label={showPin ? STR.auth.hidePin : STR.auth.showPin}
            aria-pressed={showPin}
          >
            {showPin ? STR.auth.hidePin : STR.auth.showPin}
          </button>
        </div>
        {mode === 'register' && (
          <>
            <label className="field-label" htmlFor="auth-confirm">
              {STR.auth.confirmPin}
            </label>
            <div className="pin-wrap">
              <input
                id="auth-confirm"
                ref={confirmRef}
                className="text-input"
                type={showConfirm ? 'text' : 'password'}
                inputMode="numeric"
                autoComplete="new-password"
                placeholder={STR.auth.confirmPinPlaceholder}
                maxLength={12}
                onChange={(e) => sanitizePin(e.target)}
                disabled={busy}
              />
              <button
                type="button"
                className="pin-toggle"
                onClick={() => setShowConfirm((v) => !v)}
                aria-label={showConfirm ? STR.auth.hidePin : STR.auth.showPin}
                aria-pressed={showConfirm}
              >
                {showConfirm ? STR.auth.hidePin : STR.auth.showPin}
              </button>
            </div>
            <label className="field-label" htmlFor="auth-shop">
              {STR.auth.shopName}
            </label>
            <input
              id="auth-shop"
              ref={shopRef}
              className="text-input"
              type="text"
              maxLength={60}
              autoComplete="organization"
              placeholder={STR.auth.shopPlaceholder}
              disabled={busy}
            />
            <label className="field-label" htmlFor="auth-invite">
              {STR.auth.inviteCode}
            </label>
            <input
              id="auth-invite"
              ref={inviteRef}
              className="text-input"
              type="text"
              autoComplete="off"
              placeholder={STR.auth.invitePlaceholder}
              disabled={busy}
            />
            <p className="hint">
              {STR.auth.inviteHelp}
              <br />
              {SUPPORT_CONTACT}
            </p>
          </>
        )}
        {error && (
          <p className="message error" role="alert">
            {error}
          </p>
        )}
        <div className="row">
          <button
            type="button"
            className="btn primary"
            onClick={() =>
              void (mode === 'login' ? handleLogin() : handleRegister())
            }
            disabled={busy}
          >
            {busy
              ? STR.auth.pleaseWait
              : mode === 'login'
                ? STR.auth.loginButton
                : STR.auth.registerButton}
          </button>
        </div>
        <div className="row">
          <button
            type="button"
            className="btn linklike"
            onClick={() => switchMode(mode === 'login' ? 'register' : 'login')}
            disabled={busy}
          >
            {mode === 'login' ? STR.auth.toRegister : STR.auth.toLogin}
          </button>
        </div>
      </section>
    </div>
  )
}
