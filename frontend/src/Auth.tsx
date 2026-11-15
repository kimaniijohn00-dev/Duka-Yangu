import { useRef, useState } from 'react'
import { ApiStatusError, login, register, type Session } from './api'
import { SUPPORT_CONTACT } from './config'

// Shown for every failed login (401) so the screen never reveals whether
// a phone number is registered.
const GENERIC_LOGIN_FAILURE = 'Wrong phone number or PIN. Try again.'
// Shown for 429 rate limits and after repeated failures.
const LOCKOUT_MESSAGE = 'Too many tries. Wait a minute before trying again.'
const REPEATED_HINT =
  'Too many wrong tries locks the account for a while — wait a bit, then try again.'

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
  // Uncontrolled inputs: values are read once at submit and never kept
  // in state, so no PIN ever sits in a JS variable, storage, or log.
  const phoneRef = useRef<HTMLInputElement>(null)
  const pinRef = useRef<HTMLInputElement>(null)
  const confirmRef = useRef<HTMLInputElement>(null)
  const shopRef = useRef<HTMLInputElement>(null)
  const inviteRef = useRef<HTMLInputElement>(null)

  function switchMode(next: 'login' | 'register'): void {
    onModeChange(next)
    setError(null)
    setFailCount(0)
  }

  function loginMessage(err: unknown): { text: string; failed: boolean } {
    if (err instanceof ApiStatusError) {
      if (err.status === 401 || err.status === 403) {
        return { text: GENERIC_LOGIN_FAILURE, failed: true }
      }
      if (err.status === 429) {
        return { text: LOCKOUT_MESSAGE, failed: true }
      }
      return { text: 'Could not log you in. Try again.', failed: false }
    }
    return {
      text: 'Cannot reach the server. Start the backend, then try again.',
      failed: false,
    }
  }

  async function handleLogin(): Promise<void> {
    const phone = phoneRef.current?.value.trim() ?? ''
    const pin = pinRef.current?.value ?? ''
    if (!phone) {
      setError('Enter your phone number, e.g. 0712 345 678.')
      return
    }
    if (!/^\d{6,12}$/.test(pin)) {
      setError('Enter your 6 to 12 digit PIN.')
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
          next >= 3 ? `${mapped.text} ${REPEATED_HINT}` : mapped.text,
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
        return LOCKOUT_MESSAGE
      }
      if (
        err.status === 422 ||
        err.status === 409 ||
        err.status === 403
      ) {
        return err.message
      }
      return 'Could not create your account. Try again.'
    }
    return 'Cannot reach the server. Start the backend, then try again.'
  }

  async function handleRegister(): Promise<void> {
    const phone = phoneRef.current?.value.trim() ?? ''
    const pin = pinRef.current?.value ?? ''
    const confirmPin = confirmRef.current?.value ?? ''
    const shop = shopRef.current?.value.trim() ?? ''
    const invite = inviteRef.current?.value.trim() ?? ''
    if (!phone) {
      setError('Enter your phone number, e.g. 0712 345 678.')
      return
    }
    if (!/^\d{6,12}$/.test(pin)) {
      setError('Choose a PIN of 6 to 12 digits — avoid 123456 and repeats.')
      return
    }
    if (pin !== confirmPin) {
      setError('The two PINs do not match. Type them again.')
      return
    }
    if (!shop) {
      setError('Give your shop a name, e.g. Amina\u2019s Duka.')
      return
    }
    if (!invite) {
      setError('Enter the invite code. Ask whoever invited you for it.')
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
      <section className="card" aria-label={mode === 'login' ? 'Log in' : 'Register'}>
        <h2>{mode === 'login' ? 'Log in' : 'Register'}</h2>
        <p className="hint">
          {mode === 'login'
            ? 'Use the phone number and PIN you registered with.'
            : 'One account per shop. No SMS needed — just pick a PIN.'}
        </p>
        <label className="field-label" htmlFor="auth-phone">
          Phone number
        </label>
        <input
          id="auth-phone"
          ref={phoneRef}
          className="text-input"
          type="tel"
          autoComplete="tel"
          placeholder="e.g. 0712 345 678"
          disabled={busy}
        />
        <label className="field-label" htmlFor="auth-pin">
          PIN
        </label>
        <input
          id="auth-pin"
          ref={pinRef}
          className="text-input"
          type="password"
          inputMode="numeric"
          autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
          placeholder="6 to 12 digits"
          maxLength={12}
          onChange={(e) => sanitizePin(e.target)}
          disabled={busy}
        />
        {mode === 'register' && (
          <>
            <label className="field-label" htmlFor="auth-confirm">
              Confirm PIN
            </label>
            <input
              id="auth-confirm"
              ref={confirmRef}
              className="text-input"
              type="password"
              inputMode="numeric"
              autoComplete="new-password"
              placeholder="Type the PIN again"
              maxLength={12}
              onChange={(e) => sanitizePin(e.target)}
              disabled={busy}
            />
            <label className="field-label" htmlFor="auth-shop">
              Shop name
            </label>
            <input
              id="auth-shop"
              ref={shopRef}
              className="text-input"
              type="text"
              maxLength={60}
              autoComplete="organization"
              placeholder="e.g. Amina’s Duka"
              disabled={busy}
            />
            <label className="field-label" htmlFor="auth-invite">
              Invite code
            </label>
            <input
              id="auth-invite"
              ref={inviteRef}
              className="text-input"
              type="text"
              autoComplete="off"
              placeholder="From whoever invited you"
              disabled={busy}
            />
            <p className="hint">
              No invite code? Ask the Duka-Yangu team for one.
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
              ? 'Please wait…'
              : mode === 'login'
                ? 'Log in'
                : 'Register'}
          </button>
        </div>
        <div className="row">
          <button
            type="button"
            className="btn linklike"
            onClick={() => switchMode(mode === 'login' ? 'register' : 'login')}
            disabled={busy}
          >
            {mode === 'login'
              ? 'New here? Register instead'
              : 'Have an account? Log in instead'}
          </button>
        </div>
      </section>
    </div>
  )
}
