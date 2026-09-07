import { useMemo } from 'react'
import { encodeQr, qrPath } from './qr'

/** The endpoint as a symbol a phone can read.
 *
 *  Renders nothing at all when the address will not fit, rather than a broken
 *  or half-drawn code: every caller shows the address as text beside this, so
 *  the affordance degrades to the thing it was a shortcut for. A QR is never
 *  the only way to read the endpoint on this screen.
 */
export function Qr({ text, label }: { text: string; label: string }) {
  const drawn = useMemo(() => {
    try {
      const code = encodeQr(text)
      return { size: code.size, path: qrPath(code) }
    } catch {
      // encodeQr throws only for a payload past version 10. Nothing to report
      // to the person standing here -- the address is on screen either way.
      return null
    }
  }, [text])

  if (!drawn) return null

  return (
    <div className="setup-qr">
      <svg
        viewBox={`0 0 ${drawn.size} ${drawn.size}`}
        shapeRendering="crispEdges"
        role="img"
        aria-label={label}
      >
        {/* #1A1917 is --ink's LIGHT value, written out on purpose: the tile it
            sits on is pinned to the light palette in both themes so the symbol
            always scans. See .setup-qr in setup.css. */}
        <path fill="#1A1917" d={drawn.path} />
      </svg>
    </div>
  )
}
