/** Writes text to the clipboard, trying the Clipboard API first.
 *
 *  `navigator.clipboard` is not enough on its own here. The gateway binds to
 *  the LAN and is served over plain HTTP, so on `http://spark-01:8080` — the
 *  address an operator actually opens — the Clipboard API is undefined
 *  outside a secure context. `localhost` is the one origin where it works,
 *  which is exactly the origin a developer tests on and nobody deploys to.
 *  The `execCommand` fallback is therefore the path that runs in production,
 *  not the legacy one. Shared by `Copyable` and the chat transcript's copy
 *  buttons so there is one clipboard strategy, not two.
 */
export async function copyToClipboard(text: string): Promise<'copied' | 'failed'> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return 'copied'
    }
  } catch {
    // Present but refused — a permissions policy, or a document that is not
    // focused. Fall through rather than reporting a failure the fallback
    // may not have.
  }
  return legacyCopy(text) ? 'copied' : 'failed'
}

/** The path that actually runs on a plain-HTTP LAN address. */
function legacyCopy(text: string): boolean {
  const area = document.createElement('textarea')
  area.value = text
  // Off-screen rather than hidden: a display:none or visibility:hidden element
  // cannot be selected, and the copy silently does nothing.
  area.setAttribute('readonly', '')
  area.style.position = 'fixed'
  area.style.top = '-1000px'
  area.style.opacity = '0'
  document.body.appendChild(area)
  try {
    area.select()
    area.setSelectionRange(0, text.length)
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    document.body.removeChild(area)
  }
}
