import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { translateNow } from '@/i18n'
import { $desktopBoot } from '@/store/boot'
import { $notifications, clearNotifications } from '@/store/notifications'
import { $gatewayState } from '@/store/session'

import { useGatewayBoot } from './use-gateway-boot'

// End-to-end-ish repro of the "remote VPS → stuck on CONNECTING, no Settings"
// bug that drives the REAL useGatewayBoot hook + REAL HermesGateway through a
// fake WebSocket we fully control. No Docker / no real port: from the desktop's
// point of view a "remote VPS" is just a WebSocket that opens once and later
// refuses to reopen, so that is exactly (and only) what we fake.
//
// The previous test (gateway-connecting-overlay.test.tsx) hand-set the stores
// and asserted the overlays; this one proves the HOOK actually PRODUCES that
// stuck store combo — closing the "inferred by reading code" gap on the
// post-boot reconnect loop.

type Listener = (ev: unknown) => void

// Minimal WebSocket stand-in implementing only what json-rpc-gateway.connect()
// touches: readyState, add/removeEventListener('open'|'error'|'close'), close().
class FakeWebSocket {
  static OPEN = 1
  static CLOSED = 3
  // Flipped by the test: 'open' = next socket connects; 'fail' = next socket
  // errors (a dead remote). Mirrors a VPS going away after the first connect.
  static mode: 'open' | 'fail' = 'open'
  static instances: FakeWebSocket[] = []

  readyState = 0
  private listeners: Record<string, Set<Listener>> = {}

  constructor(public url: string) {
    FakeWebSocket.instances.push(this)
    const willOpen = FakeWebSocket.mode === 'open'
    // Resolve on the next microtask/macrotask so connect()'s promise wiring is
    // in place before open/error fires (matches real async socket handshake).
    setTimeout(() => {
      if (willOpen) {
        this.readyState = FakeWebSocket.OPEN
        this.emit('open', {})
      } else {
        this.readyState = FakeWebSocket.CLOSED
        this.emit('error', {})
      }
    }, 0)
  }

  addEventListener(type: string, fn: Listener) {
    ;(this.listeners[type] ??= new Set()).add(fn)
  }

  removeEventListener(type: string, fn: Listener) {
    this.listeners[type]?.delete(fn)
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED
    // A real WebSocket fires its 'close' event asynchronously — by the time it
    // lands, JsonRpcGatewayClient.close() has already nulled this.socket, so the
    // client's close listener short-circuits and can't be relied on to flip
    // state. Model that timing here so the test can't mask a client that fails
    // to transition connectionState off 'open' after an explicit close().
    setTimeout(() => this.emit('close', {}), 0)
  }

  // Force-drop an open socket, as a sleeping laptop / restarted remote would.
  drop() {
    this.readyState = FakeWebSocket.CLOSED
    setTimeout(() => this.emit('close', {}), 0)
  }

  private emit(type: string, ev: unknown) {
    for (const fn of this.listeners[type] ?? []) {
      fn(ev)
    }
  }
}

function fakeDesktop() {
  const conn = {
    authMode: 'token' as const,
    baseUrl: 'https://vps.example.com',
    profile: 'default',
    token: 't',
    wsUrl: 'wss://vps.example.com/api/ws?token=t'
  }

  return {
    getConnection: vi.fn(async () => conn),
    getGatewayWsUrl: vi.fn(async () => conn.wsUrl),
    getBootProgress: vi.fn(async () => ({
      error: null,
      fakeMode: false,
      message: '',
      phase: 'init',
      progress: 0,
      running: true,
      timestamp: Date.now()
    })),
    onBootProgress: vi.fn(() => () => undefined),
    onBackendExit: vi.fn(() => () => undefined),
    onPowerResume: vi.fn(() => () => undefined),
    onWindowStateChanged: vi.fn(() => () => undefined),
    touchBackend: vi.fn(async () => undefined),
    profile: { get: vi.fn(async () => ({ profile: 'default' })) }
  }
}

function Harness({
  refreshHermesConfig = async () => undefined,
  refreshSessions = async () => undefined
}: {
  refreshHermesConfig?: () => Promise<void>
  refreshSessions?: () => Promise<void>
} = {}) {
  useGatewayBoot({
    handleGatewayEvent: () => undefined,
    onConnectionReady: () => undefined,
    onGatewayReady: () => undefined,
    refreshHermesConfig,
    refreshSessions
  })

  return null
}

const originalWebSocket = globalThis.WebSocket

beforeEach(() => {
  vi.useFakeTimers()
  FakeWebSocket.mode = 'open'
  FakeWebSocket.instances = []
  ;(globalThis as { WebSocket: unknown }).WebSocket = FakeWebSocket
  ;(window as { hermesDesktop?: unknown }).hermesDesktop = fakeDesktop()
  clearNotifications()
  $gatewayState.set('idle')
  $desktopBoot.set({
    error: null,
    fakeMode: false,
    message: '',
    phase: 'init',
    progress: 0,
    running: true,
    timestamp: Date.now(),
    visible: true
  })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  ;(globalThis as { WebSocket: unknown }).WebSocket = originalWebSocket
  delete (window as { hermesDesktop?: unknown }).hermesDesktop
})

// Let pending microtasks (awaits) AND the queued 0ms socket open/error fire.
async function flushAsync() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
}

// Drive the exponential backoff forward by its full cap so the next scheduled
// reconnect attempt actually runs (1s,2s,4s,8s,15s,15s…). Returns after the
// attempt's async work settles.
async function advanceBackoff() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(15_000)
  })
}

describe('useGatewayBoot remote reconnect loop (real hook, fake socket)', () => {
  it('INITIAL boot against a dead VPS: getConnection hangs (waitForHermes) → app sits in the connecting combo, then fails', async () => {
    // The report's actual path: a fresh launch pointed at an unreachable VPS.
    // startHermes()'s remote branch awaits waitForHermes() for 45s before it
    // throws, so the renderer's `await desktop.getConnection()` stays pending
    // that whole window. During it: gatewayState is still 'idle' (connect was
    // never reached) and boot.error is null → connecting=true → the fullscreen
    // CONNECTING overlay, latched, blocking Settings.
    let rejectConn: (e: Error) => void = () => undefined
    const desktop = fakeDesktop()
    desktop.getConnection = vi.fn(
      () =>
        new Promise((_resolve, reject) => {
          rejectConn = reject
        })
    )
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = desktop

    render(<Harness />)
    await flushAsync()

    // getConnection is still pending — the dead-VPS wait. No socket was ever
    // created, gatewayState never left idle, boot.error is null.
    expect(FakeWebSocket.instances).toHaveLength(0)
    expect($gatewayState.get()).not.toBe('open')
    expect($desktopBoot.get().error).toBeNull()
    // ^ connecting === true here → fullscreen CONNECTING, no Settings.

    // After ~45s waitForHermes gives up and getConnection rejects → boot()
    // catch → failDesktopBoot → the BootFailureOverlay recovery surface.
    await act(async () => {
      rejectConn(new Error('Hermes backend did not become ready: timeout'))
      await vi.advanceTimersByTimeAsync(0)
    })

    expect($desktopBoot.get().error).toBeTruthy()
  })

  it('a remote that drops post-boot keeps looping with NO boot.error (the dead-end CONNECTING combo)', async () => {
    render(<Harness />)
    await flushAsync()

    // Initial boot connected.
    expect($gatewayState.get()).toBe('open')
    expect($desktopBoot.get().error).toBeNull()
    expect(FakeWebSocket.instances).toHaveLength(1)

    // The remote VPS goes away: drop the live socket, and make every reopen
    // fail from here on.
    FakeWebSocket.mode = 'fail'
    act(() => FakeWebSocket.instances[0].drop())
    await flushAsync()

    // Burn a couple backoff cycles BEFORE the escalation threshold (<6 attempts,
    // ~the first ~15s). This is the window where stock and fixed behave the
    // same: socket down, hook retrying, gatewayState non-open, boot.error still
    // null → CONNECTING covers the screen with no recovery surface. (Past ~45s
    // the fix raises boot.error; that's asserted in the next test.)
    await advanceBackoff()

    expect($gatewayState.get()).not.toBe('open')
    expect($desktopBoot.get().error).toBeNull()
    // It is actively retrying, not idle — more sockets were minted.
    expect(FakeWebSocket.instances.length).toBeGreaterThan(1)
  })

  it('FIX: after the prolonged drop the hook raises a recoverable boot error (the escape hatch)', async () => {
    render(<Harness />)
    await flushAsync()
    expect($desktopBoot.get().error).toBeNull()

    FakeWebSocket.mode = 'fail'
    act(() => FakeWebSocket.instances[0].drop())
    await flushAsync()

    // Walk the backoff past the >=6 attempt threshold (~45s of failures).
    for (let i = 0; i < 8; i += 1) {
      await advanceBackoff()
    }

    // The hook surfaced the recoverable error → BootFailureOverlay (Use local
    // gateway / Sign in / Retry) becomes reachable instead of CONNECTING.
    expect($desktopBoot.get().error).toBeTruthy()
  })

  it('FIX: a successful reconnect clears the recoverable error', async () => {
    render(<Harness />)
    await flushAsync()

    FakeWebSocket.mode = 'fail'
    act(() => FakeWebSocket.instances[0].drop())
    await flushAsync()

    for (let i = 0; i < 8; i += 1) {
      await advanceBackoff()
    }

    expect($desktopBoot.get().error).toBeTruthy()

    // The remote comes back: next reconnect attempt opens.
    FakeWebSocket.mode = 'open'
    await advanceBackoff()

    expect($gatewayState.get()).toBe('open')
    expect($desktopBoot.get().error).toBeNull()
  })

  it('FIX: a reconnect whose resync FAILS does not clear the error (no ready UI over stale data)', async () => {
    // The socket can reopen while the backend still cannot serve config/sessions
    // (mid-restart). Completing the boot then would paint a "ready" UI over
    // stale/empty data — so a failed resync must NOT clear the recovery surface.
    let allowResync = true

    const refreshSessions = vi.fn(async () => {
      if (!allowResync) {
        throw new Error('backend mid-restart: sessions unavailable')
      }
    })

    render(<Harness refreshSessions={refreshSessions} />)
    await flushAsync()

    // Surface the recoverable error via a prolonged drop (as the prior test).
    FakeWebSocket.mode = 'fail'
    act(() => FakeWebSocket.instances[0].drop())
    await flushAsync()

    for (let i = 0; i < 8; i += 1) {
      await advanceBackoff()
    }

    expect($desktopBoot.get().error).toBeTruthy()

    // The remote's SOCKET returns, but the backend can't serve the resync yet.
    FakeWebSocket.mode = 'open'
    allowResync = false
    await advanceBackoff()

    // Socket opened, but because the resync failed the error must remain — the
    // hook does not falsely report the desktop as ready.
    expect($desktopBoot.get().error).toBeTruthy()

    // Backend finishes restarting: a later reconnect's resync succeeds → cleared.
    allowResync = true
    await advanceBackoff()
    await advanceBackoff()
    expect($gatewayState.get()).toBe('open')
    expect($desktopBoot.get().error).toBeNull()
  })

  it('FIX: a socket that reopens but keeps failing resync backs off instead of hammering at ~1Hz', async () => {
    // The backend accepts the WebSocket but 503s its RPCs while mid-restart, so
    // every cycle is open → resync-fail → close. onState('open') resets
    // reconnectAttempt each cycle, so a backoff keyed only on reconnectAttempt
    // would stay flat at ~1s and pound a fragile backend with a reconnect +
    // double-RPC storm. The resyncFailures counter must drive growth instead.
    let allowResync = true

    const refreshSessions = vi.fn(async () => {
      if (!allowResync) {
        throw new Error('backend mid-restart: sessions unavailable')
      }
    })

    render(<Harness refreshSessions={refreshSessions} />)
    await flushAsync()
    expect($gatewayState.get()).toBe('open')

    // The socket will keep reopening, but the resync now fails every time.
    allowResync = false
    act(() => FakeWebSocket.instances[0].drop())
    await flushAsync()

    const before = FakeWebSocket.instances.length
    // Over a 60s window a flat ~1s backoff would mint ~30-60 sockets; the
    // resyncFailures-driven backoff ramps to the 15s cap, so only a handful do.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    const minted = FakeWebSocket.instances.length - before

    expect(minted).toBeGreaterThan(0) // it IS still retrying, not wedged
    expect(minted).toBeLessThan(12) // …but with growing backoff, not ~1Hz
  })

  it('FIX: repeated wake signals during a sustained reauth outage do not stack sign-in toasts', async () => {
    // The one-shot reauth guard exists to surface "sign in again" ONCE per
    // disconnect episode, not once per attempt — a dead OAuth ticket fails every
    // reconnect, and re-firing the toast (+ its haptics) on each wake signal is
    // exactly the spam it must prevent. An episode ends on a clean socket open,
    // NOT on a wake nudge, so clustered visibility/online events during the same
    // outage must not re-arm it.
    render(<Harness />)
    await flushAsync()
    expect($gatewayState.get()).toBe('open')

    const desktop = window.hermesDesktop as unknown as ReturnType<typeof fakeDesktop>
    desktop.getConnection = vi.fn(async () => {
      throw Object.assign(new Error('socket closed'), { needsOauthLogin: true })
    })

    FakeWebSocket.mode = 'fail'
    act(() => FakeWebSocket.instances[0].drop())
    await flushAsync()

    // First failed reconnect → exactly one sign-in toast.
    await advanceBackoff()
    const reauthTitle = translateNow('boot.errors.gatewaySignInRequired')
    const countReauthToasts = () => $notifications.get().filter(n => n.title === reauthTitle).length
    expect(countReauthToasts()).toBe(1)

    // The user alt-tabs / the network flaps repeatedly during the SAME outage.
    for (let i = 0; i < 5; i += 1) {
      act(() => window.dispatchEvent(new Event('online')))
      await flushAsync()
    }

    // Still exactly one — the guard held across every wake signal.
    expect(countReauthToasts()).toBe(1)
  })

  it('FIX: a reauth failure surfaces the actionable sign-in copy, not the raw transport string', async () => {
    render(<Harness />)
    await flushAsync()
    expect($gatewayState.get()).toBe('open')

    // The remote drops and the OAuth ticket is dead: every reconnect now fails
    // with a reauth-required error (needsOauthLogin). Same desktop object the
    // hook captured, so overriding getConnection is visible to it.
    const desktop = window.hermesDesktop as unknown as ReturnType<typeof fakeDesktop>
    desktop.getConnection = vi.fn(async () => {
      throw Object.assign(new Error('socket closed'), { needsOauthLogin: true })
    })

    FakeWebSocket.mode = 'fail'
    act(() => FakeWebSocket.instances[0].drop())
    await flushAsync()

    for (let i = 0; i < 8; i += 1) {
      await advanceBackoff()
    }

    // Block B surfaced the actionable sign-in message on the recovery overlay,
    // not the opaque 'socket closed' transport string.
    expect($desktopBoot.get().error).toBe(translateNow('boot.errors.gatewaySignInRequired'))
  })
})
