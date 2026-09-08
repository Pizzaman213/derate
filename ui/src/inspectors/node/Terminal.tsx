import { useEffect, useRef, useState } from 'react'
import type { FitAddon } from '@xterm/addon-fit'
import type { Terminal as Xterm } from '@xterm/xterm'
import { wsUrl } from '../../api/origin'
import { useShellStatus } from '../../state/history'
import { Verbatim } from '../../components/Verbatim'

// The one dependency this UI takes that it could not have written, and it is
// **loaded on demand**, not bundled. Both halves of that matter.
//
// The dependency: `agents/H-ui.md` says "do not add a chart library for one
// line graph", and Chart.tsx honours it by building SVG paths by hand. A
// terminal is the opposite case. Rendering a pty means implementing VT100 --
// alternate screen buffers, scroll regions, wide characters, mouse reporting --
// and the version of that which fits in a few hundred lines renders `top` and
// `vim` wrong. Those are most of what a node shell is for.
//
// The laziness: xterm is 118 kB gzipped against an app that is 115 kB, so
// bundling it would roughly double what every visitor downloads for a panel
// most sessions never open. A dynamic import makes Vite emit it as its own
// chunk, fetched the first time somebody actually opens a shell and never
// otherwise. The type-only imports above are erased at compile time and pull
// in nothing.
//
// It is also why no terminal byte goes through React state. The chat
// transcript re-maps its whole turn array per delta frame -- fine at token
// rates, and it would melt at the throughput of a `find /`. xterm owns its own
// DOM; this component owns the socket and gets out of the way.

const SUBPROTOCOL = 'derate-shell'

type Phase = 'closed' | 'loading' | 'connecting' | 'open' | 'ended'

/** A WebSocket subprotocol is an RFC 6455 token, so a key containing a space,
 *  a comma or a quote cannot be carried in one at all. Checked here so the
 *  answer is a sentence rather than a handshake that fails for no stated
 *  reason. (Kept from the parallel implementation this replaced -- it is a
 *  real edge the xterm half had missed.) */
function keyIsCarriable(key: string): boolean {
  return /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/.test(key)
}

export function NodeTerminal({ nodeId }: { nodeId: string }) {
  const status = useShellStatus()
  const [phase, setPhase] = useState<Phase>('closed')
  const [key, setKey] = useState('')
  const [error, setError] = useState<string | null>(null)

  const mountRef = useRef<HTMLDivElement>(null)
  const termRef = useRef<Xterm | null>(null)
  const fitRef = useRef<FitAddon | null>(null)
  const socketRef = useRef<WebSocket | null>(null)

  // Tear down when the sheet closes or the node changes. Without this,
  // switching machines leaves the previous socket open -- and on the far side
  // that is a root shell nobody is looking at any more.
  useEffect(() => {
    return () => {
      socketRef.current?.close()
      termRef.current?.dispose()
      socketRef.current = null
      termRef.current = null
      fitRef.current = null
    }
  }, [nodeId])

  const open = async () => {
    if (phase !== 'closed' && phase !== 'ended') return
    if (!keyIsCarriable(key)) {
      setError(
        'That key cannot be sent. It travels as a WebSocket subprotocol, which allows letters, digits and -._~ but not spaces, commas or quotes.',
      )
      return
    }
    setError(null)
    setPhase('loading')

    let Xterm_: typeof Xterm
    let FitAddon_: typeof FitAddon
    try {
      // Three chunks, fetched once and cached by the browser thereafter.
      const [core, fit] = await Promise.all([
        import('@xterm/xterm'),
        import('@xterm/addon-fit'),
        import('@xterm/xterm/css/xterm.css'),
      ])
      Xterm_ = core.Terminal
      FitAddon_ = fit.FitAddon
    } catch {
      setPhase('closed')
      setError(
        'The terminal could not be loaded. It is fetched separately the first time it is opened, so this is usually a network problem between here and the coordinator.',
      )
      return
    }

    setPhase('connecting')
    // The mount point only exists once phase left 'closed', so wait a frame for
    // React to commit it before xterm measures anything.
    await new Promise((r) => requestAnimationFrame(r))
    if (!mountRef.current) {
      setPhase('closed')
      return
    }

    const term = new Xterm_({
      fontFamily: "'IBM Plex Mono', ui-monospace, monospace",
      fontSize: 12,
      // From the page's own tokens rather than hardcoded, so the terminal is
      // part of the instrument in both themes rather than a black rectangle
      // dropped into a cream panel.
      theme: readTheme(),
      cursorBlink: true,
      scrollback: 5000,
    })
    const fit = new FitAddon_()
    term.loadAddon(fit)
    term.open(mountRef.current)
    fit.fit()
    termRef.current = term
    fitRef.current = fit

    const socket = new WebSocket(wsUrl(`/api/nodes/${encodeURIComponent(nodeId)}/shell`), [
      SUBPROTOCOL,
      key,
    ])
    socket.binaryType = 'arraybuffer'
    socketRef.current = socket

    const send = (frame: object) => {
      if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(frame))
    }

    socket.onopen = () => {
      setPhase('open')
      send({ r: [term.cols, term.rows] })
      term.focus()
    }
    // Straight through, no decode. An escape sequence can split across two
    // frames, so anything that tried to be helpful about encoding here would
    // corrupt the stream.
    socket.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) term.write(new Uint8Array(event.data))
      else term.write(String(event.data))
    }
    socket.onclose = (event) => {
      setPhase('ended')
      // The node's own sentence when it sent one: a refusal happens before the
      // socket is accepted, so the close reason is how it reaches this page.
      if (event.reason) setError(event.reason)
      else if (event.code !== 1000 && event.code !== 1005) {
        setError(
          'The connection closed before a session started. The node may have the shell switched off, or the key may be wrong.',
        )
      }
    }

    term.onData((data) => send({ i: data }))
    term.onResize(({ cols, rows }) => send({ r: [cols, rows] }))

    const onResize = () => {
      try {
        fitRef.current?.fit()
      } catch {
        // Not laid out yet, or the sheet is closing.
      }
    }
    window.addEventListener('resize', onResize)
    socket.addEventListener('close', () => window.removeEventListener('resize', onResize))
  }

  const detach = () => {
    socketRef.current?.close()
    setPhase('ended')
  }

  const close = () => {
    socketRef.current?.close()
    termRef.current?.dispose()
    termRef.current = null
    fitRef.current = null
    setPhase('closed')
    setError(null)
  }

  if (status.data && !status.data.enabled) {
    return (
      <>
        <div className="sub">terminal</div>
        <div className="unit">
          {status.data.reason ||
            'The shell is switched off. Set DERATE_SHELL=1 on the coordinator and on the node to turn it on.'}
        </div>
      </>
    )
  }

  return (
    <>
      <div className="sub">terminal</div>

      {phase === 'closed' || phase === 'loading' ? (
        <>
          {/* Deliberately does not say "root". What you get is whatever user
              the node agent runs as, and whether it reaches the host depends on
              --pid=host -- so on a container node this is root on the machine,
              and on a coordinator started as a bare process it is that user in
              that process's own namespaces. Asserting the worst case would be
              wrong on half the fleet, and this product's rule about not
              rendering a figure it did not measure applies to a claim about
              privilege at least as much as to a number. The prompt says which
              once it opens. */}
          <div className="unit" style={{ marginBottom: 8 }}>
            A shell on {nodeId}, running as whatever user its node agent runs as — on a node
            started with <span className="mono">--pid=host</span> that is root, on the machine
            itself rather than in the container. The connection is not encrypted: the coordinator
            is served over plain HTTP on a LAN address, so the key and everything typed cross the
            network in clear. Where the node&apos;s container mounts the operator&apos;s SSH keys,
            a session here also reaches every machine those keys reach.
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 10 }}>
            <label className="sr-only" htmlFor={`shell-key-${nodeId}`}>
              Shell key
            </label>
            <input
              id={`shell-key-${nodeId}`}
              type="password"
              value={key}
              placeholder="shell key"
              autoComplete="off"
              onChange={(e) => setKey(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && key) void open()
              }}
              style={{ width: 240 }}
            />
            <button onClick={() => void open()} disabled={!key || phase === 'loading'}>
              {phase === 'loading' ? 'Loading…' : 'Open a shell'}
            </button>
            <span className="unit">
              DERATE_SHELL_KEY on the node, or {'<data dir>'}/shell.key
            </span>
          </div>
        </>
      ) : null}

      {phase === 'connecting' || phase === 'open' || phase === 'ended' ? (
        <div className="logbox">
          <h4>
            <span>
              {nodeId}
              <span className="unit">
                {' '}
                ·{' '}
                {phase === 'open'
                  ? 'connected'
                  : phase === 'connecting'
                    ? 'connecting…'
                    : 'session ended'}
              </span>
            </span>
            <span className="chips">
              {phase === 'open' ? (
                <button onClick={detach}>Detach</button>
              ) : (
                <button onClick={close}>Close</button>
              )}
            </span>
          </h4>
          <div className="termbody" ref={mountRef} />
        </div>
      ) : null}

      {error ? (
        <div style={{ marginTop: 6 }}>
          <Verbatim text={error} size="unit" />
        </div>
      ) : null}
    </>
  )
}

/** xterm needs literal colours, and this page is themed with custom properties
 *  that change under `prefers-color-scheme` and the theme switch. Read from the
 *  live computed style so the terminal matches whichever is in force. */
function readTheme() {
  const style = getComputedStyle(document.documentElement)
  const token = (name: string, fallback: string) =>
    style.getPropertyValue(name).trim() || fallback
  return {
    background: token('--panel', '#EDE9E0'),
    foreground: token('--ink', '#1A1917'),
    cursor: token('--ink', '#1A1917'),
    selectionBackground: token('--panel-sunk', '#D6D0C2'),
    red: token('--fault', '#9A3327'),
    green: token('--live', '#2F6E3A'),
    yellow: token('--warn', '#7F5C14'),
    blue: token('--flow', '#4A6FA5'),
  }
}
