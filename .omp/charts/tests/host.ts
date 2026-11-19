// A sized container for jsdom, which has no layout of its own.
export function host(width = 640, height = 400): HTMLDivElement {
  const div = document.createElement('div')
  document.body.appendChild(div)
  Object.defineProperty(div, 'clientWidth', { configurable: true, get: () => width })
  Object.defineProperty(div, 'clientHeight', { configurable: true, get: () => (height ? height : div.scrollHeight) })
  return div
}
