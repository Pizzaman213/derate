# check

Shared machinery for the verifier suite. Neither file here is a verifier:
`harness.mjs` is the prelude a new `*.check.mjs` starts from, so writing one
begins at its first assertion rather than at thirty lines of bundling and
counting, and `browser.mjs` answers "where is the Chromium" without ever
reaching for the network. The runner, `ui/check.mjs`, imports `browser.mjs`
and only `browser.mjs`; `harness.mjs` is imported by verifiers alone.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `harness.mjs` | 82 | `load()` — esbuild a TS module so node can import it — and `report()`, which prints PASS/FAIL, counts, and sets the exit code |
| `browser.mjs` | 51 | `findBrowser()`: a Chromium already on this machine, or an honest reason there is none |

## `harness.mjs`

Two exports. `load(callerUrl, rel)` bundles a TypeScript module with esbuild
and returns it, resolved relative to the calling verifier; `report()` returns
`{ check, note, done }`.

Both existed fifteen times over, in two different idioms, before this file did.
There is no test runner in `ui/` — the `*.check.mjs` files **are** the suite —
and the reason there were not more of them is that the first thirty lines of a
new one were always the same two chores. A verifier now opens with:

```js
import { load, report } from '../../check/harness.mjs'
const M = await load(import.meta.url, './rows.ts')
const { check, note, done } = report()
check(M.rows([]).length === 0, 'an empty registry has no rows')
done()
```

`check(ok, what)` prints and counts. `note(what)` prints an indented line and
is **never** counted — it is for a measured number or a caveat about what the
live data happened to contain, not for an assertion. `done()` prints the tally
and exits.

Three verifiers import it today — `shell/screens.check.mjs`,
`tabs/chat/tags.check.mjs`, `tabs/chat/voices.check.mjs` — out of twenty-one in
`src/`. Seventeen of the remaining eighteen still carry their own esbuild call.
The eighteenth, `tabs/settings/allowlist.check.mjs`, bundles nothing at all: it
asserts against the live coordinator over `fetch` and hand-rolls its own `check`
and failure counter.

## `browser.mjs`

`findBrowser()` returns `{ path, why }` and never throws. `why` is populated on
both outcomes, and it is a label rather than a path: the cache entry the
binary was found under (`chromium-1234`, which is what this box has), or the
literal `$DERATE_CHECK_BROWSER` when the variable named it, or the sentence
explaining that there is no browser. Callers print it — they never parse it.

The order is `$DERATE_CHECK_BROWSER` first — an explicit path that does not
exist is reported as `$DERATE_CHECK_BROWSER=<path> does not exist`, not silently
fallen past — then `$PLAYWRIGHT_BROWSERS_PATH` or `~/.cache/ms-playwright`,
scanning `chromium-*` before `chromium_headless_shell-*` and looking for
`chrome-linux/chrome` or `chrome-linux/headless_shell` inside each. The full
browser wins when both are present, because the headless shell is smaller and
faster but cannot do everything, and the screens capture is supposed to show
what a person would see.

**Nothing is ever downloaded.** A check that reaches for the network to decide
whether it can run fails for reasons that have nothing to do with the code.

## The seam with the runner and the verifiers

`ui/check.mjs` imports `findBrowser` and nothing else from here; it is the
`browser` probe, asked once per run rather than once per verifier, and a
verifier opts into it with `// requires: browser` on its first line.
`shell/screens.check.mjs` imports both files — `findBrowser` for the
executable, `load` to pull `state/routes.ts` so the destinations it walks are
enumerated from `DESTINATIONS` rather than listed in the verifier.

```js
import { findBrowser } from '../check/browser.mjs'
const browser = findBrowser()          // { path, why }
note(`chromium: ${browser.why}`)       // the cache dir, printed, never asserted
```

**Nothing here knows about the runner.** A verifier stays a program you can run
on its own — `node src/tabs/models/rows.check.mjs` — because that is the inner
loop, and a harness that only worked under `npm run check` would have taken it
away.

## Things that look like details and are not

**`platform: 'neutral'` in the esbuild call is a rule, not a setting.** A
module that reached for a node built-in would fail at `load()` rather than
passing a check it could never pass in a browser. The suite exists to catch
what `tsc` cannot see; a bundle that quietly polyfilled node would put a whole
class of that back.

**`write: false` plus a base64 data URI, rather than a temp file.** Nothing is
left on disk to clean up, and no verifier has to own a teardown path.

**esbuild rather than a plain `import`.** Node's ESM resolver will not resolve
the extensionless specifiers these modules use for each other
(`from '../../api/types'`), so a direct import of the TS module fails before
any assertion runs.

**`done()` exits non-zero when nothing was checked at all.** A verifier whose
assertions were all skipped past by a `for` loop over an empty list is the same
lie as a green gate that ran nothing, and it is a lie this suite has told
before. The message says so: `no checks ran -- a verifier that asserts nothing
is not a pass`.

**`revision()` sorts the cache numerically, not as text.** It strips the
leading non-digits and compares numbers, so `chromium-1234` sorts after
`chromium-999`. A string sort gets that backwards and would quietly pick a
browser years older than the one installed for this.

**The dependency is `playwright-core`, not `playwright`.** `playwright-core`
never downloads anything, and passing an explicit `executablePath` means the
driver's version does not have to match the cached browser's revision. It is
`^1.63.0` in `ui/package.json` and `shell/screens.check.mjs` is what imports it
— `browser.mjs` itself pulls in nothing but `node:fs`, `node:os` and
`node:path`, so asking where the browser is costs no driver startup.

## Failure behaviour

- **No browser anywhere.** `{ path: null, why }`. The caller decides what that
  means: `screens.check.mjs` prints the reason and exits 1, while `check.mjs`
  turns it into a SKIP in its own column — and into a FAIL under
  `--strict`. A skip is never folded into the passes.
- **`$DERATE_CHECK_BROWSER` set to a path that is not there.** Reported by name
  and value rather than falling through to the cache, because an operator who
  set that variable meant it.
- **No cache directory at all.** `no browser cache at <path>`, naming the
  directory that was looked in.
- **A cache with no Chromium in it.** `no chromium under <path>` — a separate
  sentence from the one above, because a cache that exists and holds only
  Firefox or WebKit is a different thing to fix than a cache that was never
  created.
- **esbuild cannot build the module.** `logLevel: 'silent'` keeps esbuild from
  printing its own report, `build()` rejects, and the unhandled rejection takes
  the verifier down non-zero. There is no branch that continues without a
  module.
- **The bundled module throws on import.** Same path — the `import()` of the
  data URI rejects and the process exits non-zero with no tally printed. Which
  side of `report()` that lands on depends on the verifier: `screens.check.mjs`
  calls `report()` first and loads `state/routes.ts` well after it, while
  `tags.check.mjs` and `voices.check.mjs` load before they report.

## Deliberately not built

**A browser download.** The screens verifier uses what is on the machine or
declines to run. Install one with Playwright, or point `$DERATE_CHECK_BROWSER`
at the binary.

**An assertion library.** `check(ok, what)` takes a boolean and a sentence.
Every message in the suite is written by the person who knew what the assertion
meant, which is what makes a failing line readable without opening the file.

**Any knowledge of `ui/check.mjs`.** The dependency runs one way. `check.mjs`
imports `findBrowser`; nothing here imports `check.mjs`, reads its flags, or
behaves differently under it.
