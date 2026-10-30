// Browser recording for Day 3B voice entry.
//
// The Meta transcribe endpoint only accepts WAV: a RIFF/WAVE container of
// mono integer PCM at 16 or 24 kHz (see backend/services/transcribe.py).
// Whatever the microphone gives us is decoded, resampled to mono 16 kHz,
// and encoded as 16-bit PCM WAV here, with no extra dependency.

export const VOICE_SAMPLE_RATE = 16000
export const VOICE_MAX_SECONDS = 60

export interface VoiceClip {
  blob: Blob
  durationSeconds: number
}

function writeAscii(view: DataView, offset: number, text: string): void {
  for (let i = 0; i < text.length; i++) {
    view.setUint8(offset + i, text.charCodeAt(i))
  }
}

/** Encode mono float samples as 16-bit PCM WAV. */
function encodeWav(samples: Float32Array, sampleRate: number): Blob {
  const buffer = new ArrayBuffer(44 + samples.length * 2)
  const view = new DataView(buffer)
  writeAscii(view, 0, 'RIFF')
  view.setUint32(4, 36 + samples.length * 2, true)
  writeAscii(view, 8, 'WAVE')
  writeAscii(view, 12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1, true)
  view.setUint16(22, 1, true)
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true)
  view.setUint16(32, 2, true)
  view.setUint16(34, 16, true)
  writeAscii(view, 36, 'data')
  view.setUint32(40, samples.length * 2, true)
  for (let i = 0; i < samples.length; i++) {
    const clamped = Math.max(-1, Math.min(1, samples[i]))
    view.setInt16(44 + i * 2, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true)
  }
  return new Blob([buffer], { type: 'audio/wav' })
}

function micDeniedMessage(err: unknown): string {
  if (err instanceof DOMException) {
    if (err.name === 'NotAllowedError' || err.name === 'SecurityError') {
      return 'The microphone is blocked. Allow mic access for this site (click the icon in the address bar), then tap the mic again.'
    }
    if (err.name === 'NotFoundError' || err.name === 'OverconstrainedError') {
      return 'No microphone found on this device. Type the entry instead.'
    }
    if (err.name === 'NotReadableError') {
      return 'The microphone is busy in another app. Close it and try again.'
    }
  }
  return 'Could not start the microphone. Type the entry instead.'
}

function pickMimeType(): string | undefined {
  const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4']
  for (const mime of candidates) {
    try {
      if (MediaRecorder.isTypeSupported(mime)) return mime
    } catch {
      // Try the next candidate.
    }
  }
  return undefined
}

function audioContextCtor(): typeof AudioContext | null {
  const w = window as unknown as {
    AudioContext?: typeof AudioContext
    webkitAudioContext?: typeof AudioContext
  }
  return w.AudioContext ?? w.webkitAudioContext ?? null
}

/** Decode any recorded audio and re-encode it as 16 kHz mono 16-bit PCM WAV. */
async function convertToWav(raw: Blob, maxSeconds: number): Promise<VoiceClip> {
  if (raw.size === 0) {
    throw new Error('Nothing was recorded. Tap the mic, speak, then tap Stop.')
  }
  const Ctor = audioContextCtor()
  if (!Ctor) {
    throw new Error('This browser cannot process audio. Type the entry instead.')
  }
  const ctx = new Ctor()
  try {
    const decoded = await ctx.decodeAudioData(await raw.arrayBuffer())
    const capped = Math.min(decoded.duration, maxSeconds)
    const offline = new OfflineAudioContext(
      1,
      Math.max(1, Math.ceil(capped * VOICE_SAMPLE_RATE)),
      VOICE_SAMPLE_RATE,
    )
    const src = offline.createBufferSource()
    src.buffer = decoded
    src.connect(offline.destination)
    src.start(0)
    const rendered = await offline.startRendering()
    return {
      blob: encodeWav(rendered.getChannelData(0), VOICE_SAMPLE_RATE),
      durationSeconds: rendered.duration,
    }
  } catch {
    throw new Error('Could not use that recording. Speak clearly and try again.')
  } finally {
    void ctx.close()
  }
}

/**
 * Tap-to-record helper. start() asks for the mic and resolves once recording;
 * stop() ends it (or the max length ends it) and resolves with a WAV clip;
 * cancel() abandons the recording and releases the mic.
 */
export class VoiceRecorder {
  private stream: MediaStream | null = null
  private recorder: MediaRecorder | null = null
  private readonly chunks: Blob[] = []
  private stopTimer: number | null = null
  private finished = false
  private readonly stopped: Promise<VoiceClip>
  private resolveStopped: ((clip: VoiceClip) => void) | null = null
  private rejectStopped: ((err: Error) => void) | null = null

  constructor(private readonly maxSeconds: number = VOICE_MAX_SECONDS) {
    this.stopped = new Promise<VoiceClip>((resolve, reject) => {
      this.resolveStopped = resolve
      this.rejectStopped = reject
    })
  }

  async start(): Promise<void> {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error(
        'Voice input needs localhost or HTTPS in a browser with a microphone — try Chrome or Edge on this PC.',
      )
    }
    if (typeof MediaRecorder === 'undefined') {
      throw new Error('This browser cannot record audio. Type the entry instead.')
    }
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch (err) {
      throw new Error(micDeniedMessage(err))
    }
    const mimeType = pickMimeType()
    this.recorder = new MediaRecorder(
      this.stream,
      mimeType ? { mimeType } : undefined,
    )
    this.recorder.ondataavailable = (event: BlobEvent): void => {
      if (event.data.size > 0) this.chunks.push(event.data)
    }
    this.recorder.onstop = (): void => {
      void this.finish()
    }
    this.recorder.onerror = (): void => {
      this.settleError('Recording failed. Type the entry instead.')
    }
    this.recorder.start()
    this.stopTimer = window.setTimeout(() => {
      this.stopRecording()
    }, this.maxSeconds * 1000)
  }

  /** Stop recording and resolve with the WAV clip. */
  stop(): Promise<VoiceClip> {
    this.stopRecording()
    return this.stopped
  }

  /** Abandon the recording and release the mic. The pending clip never resolves. */
  cancel(): void {
    this.finished = true
    this.release()
  }

  private stopRecording(): void {
    if (this.stopTimer !== null) {
      window.clearTimeout(this.stopTimer)
      this.stopTimer = null
    }
    if (this.recorder && this.recorder.state !== 'inactive') {
      this.recorder.stop()
    } else {
      void this.finish()
    }
  }

  private async finish(): Promise<void> {
    if (this.finished) return
    this.finished = true
    const chunks = this.chunks.splice(0)
    const maxSeconds = this.maxSeconds
    this.release()
    try {
      this.resolveStopped?.(await convertToWav(new Blob(chunks), maxSeconds))
    } catch (err) {
      this.settleError(err instanceof Error ? err.message : 'Voice input failed.')
    }
  }

  private settleError(message: string): void {
    if (this.finished) return
    this.finished = true
    this.release()
    this.rejectStopped?.(new Error(message))
  }

  private release(): void {
    if (this.stopTimer !== null) {
      window.clearTimeout(this.stopTimer)
      this.stopTimer = null
    }
    this.recorder = null
    if (this.stream) {
      for (const track of this.stream.getTracks()) track.stop()
      this.stream = null
    }
  }
}
