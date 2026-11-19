export {}
// jsdom gaps, filled honestly: what is stubbed here does not exist in jsdom, and
// everything that depends on real layout or pixels is not tested here.
// Nothing here changes chart behaviour.

class TrackedResizeObserver {
  static live = new Set<TrackedResizeObserver>()
  targets = new Set<Element>()
  constructor(public cb: ResizeObserverCallback) {}
  observe(el: Element) {
    this.targets.add(el)
    TrackedResizeObserver.live.add(this)
  }
  unobserve(el: Element) {
    this.targets.delete(el)
    if (!this.targets.size) TrackedResizeObserver.live.delete(this)
  }
  disconnect() {
    this.targets.clear()
    TrackedResizeObserver.live.delete(this)
  }
  /** Test hook: deliver a resize to every observer of `el`. */
  static fire(el: Element, width: number, height: number) {
    for (const ro of TrackedResizeObserver.live)
      if (ro.targets.has(el))
        ro.cb([{ target: el, contentRect: { width, height, x: 0, y: 0, top: 0, left: 0, right: width, bottom: height } } as unknown as ResizeObserverEntry], ro as unknown as ResizeObserver)
  }
}
declare global {
  // eslint-disable-next-line no-var
  var __ro: typeof TrackedResizeObserver
}
// jsdom has no ResizeObserver; this one records observe/disconnect for the leak checks.
globalThis.ResizeObserver = TrackedResizeObserver as unknown as typeof ResizeObserver
globalThis.__ro = TrackedResizeObserver

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}

// jsdom has no 2D canvas; it logs "Not implemented" on every call. Return null quietly --
// core treats a null context as "no raster here" and still lays out and counts marks.
HTMLCanvasElement.prototype.getContext = (() => null) as unknown as HTMLCanvasElement['getContext']
