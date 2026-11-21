import { useCallback, useState, type KeyboardEvent, type PointerEvent, type RefObject } from 'react'

/** A SHARE of the split, not pixels: a remembered 760px was half of a laptop
 *  and a sixth of a zoomed-out 4K window, where the detail pane sat in a
 *  narrow strip beside a list stretched across the rest. The old key held
 *  pixels and is left unread. */
const KEY = 'derate.models.sideShare'
const DEFAULT = 0.5
const MIN_SHARE = 0.25
const MAX_SHARE = 0.75
/** Whatever the share, neither pane goes below these. CSS enforces the same
 *  floors (`.mdl-split`), so a stored share can never squeeze a pane. */
const SIDE_MIN = 420
const LIST_MIN = 440
/** Half the handle's own column, so the pointer stays on the seam it grabbed. */
const GRAB = 7

function clampShare(share: number, container: number | null): number {
  let lo = MIN_SHARE
  let hi = MAX_SHARE
  if (container) {
    lo = Math.max(lo, SIDE_MIN / container)
    hi = Math.min(hi, (container - LIST_MIN) / container)
  }
  return Math.round(Math.max(lo, Math.min(hi, share)) * 1000) / 1000
}

/** The detail pane's share of the split, remembered across visits. */
export function useSideWidth(): [number, (share: number) => void] {
  const [share, setShare] = useState(() => {
    const stored = Number(window.localStorage.getItem(KEY))
    return Number.isFinite(stored) && stored >= MIN_SHARE && stored <= MAX_SHARE ? stored : DEFAULT
  })
  const set = useCallback((s: number) => {
    setShare(s)
    window.localStorage.setItem(KEY, String(s))
  }, [])
  return [share, set]
}

/** The seam between the list and the detail pane: drag it, or focus it and
 *  use the arrow keys. A real `separator` with its value exposed, because a
 *  pointer-only resize is a width nobody on a keyboard can change. */
export function SplitHandle({
  width,
  onWidth,
  container,
}: {
  /** The detail pane's share of the split, 0..1. */
  width: number
  onWidth: (share: number) => void
  container: RefObject<HTMLElement>
}) {
  const box = () => container.current?.getBoundingClientRect() ?? null

  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return
    e.preventDefault()
    const el = e.currentTarget
    el.setPointerCapture(e.pointerId)
    const move = (ev: globalThis.PointerEvent) => {
      const r = box()
      if (!r || !r.width) return
      onWidth(clampShare((r.right - ev.clientX - GRAB) / r.width, r.width))
    }
    const up = () => {
      el.removeEventListener('pointermove', move)
      el.removeEventListener('pointerup', up)
      el.removeEventListener('pointercancel', up)
    }
    el.addEventListener('pointermove', move)
    el.addEventListener('pointerup', up)
    el.addEventListener('pointercancel', up)
  }

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const step = e.shiftKey ? 0.08 : 0.02
    const r = box()
    let next: number | null = null
    // The pane is on the right, so moving the seam left widens it.
    if (e.key === 'ArrowLeft') next = width + step
    else if (e.key === 'ArrowRight') next = width - step
    else if (e.key === 'Home') next = MAX_SHARE
    else if (e.key === 'End') next = MIN_SHARE
    if (next == null) return
    e.preventDefault()
    onWidth(clampShare(next, r?.width ?? null))
  }

  return (
    <div
      className="mdl-handle"
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize the detail pane"
      aria-valuemin={Math.round(MIN_SHARE * 100)}
      aria-valuemax={Math.round(MAX_SHARE * 100)}
      aria-valuenow={Math.round(width * 100)}
      aria-valuetext={`detail pane ${Math.round(width * 100)}% of the width`}
      tabIndex={0}
      title="Drag to resize. Double-click to reset."
      onPointerDown={onPointerDown}
      onKeyDown={onKeyDown}
      onDoubleClick={() => onWidth(clampShare(DEFAULT, box()?.width ?? null))}
    />
  )
}
