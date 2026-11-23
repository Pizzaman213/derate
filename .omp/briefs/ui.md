# UI brief

Owns: `ui/`, the screen over `/api`: URL scheme, polled state, themed components, the verifier suite; and the design pack in `.omp/design` that the screen is built from.
Code: ui/src (api, state, components, shell, sidebar, tabs, inspectors, styles, check), ui/check.mjs, .omp/design   Tests: ui/src/**/*.check.mjs, ui/check.mjs   Read first: `ui/README.md` (sections URLs, Things that look like details and are not, `check.mjs`), `.omp/RULES.md`, `ui/src/components/README.md`

An agent reads Depth 1 for every part it touches, and goes deeper only as far as the task needs.
Every `path::symbol` reference below is verified by the gate, so the brief cannot name code that
does not exist. Long form: `ui/README.md`, `ui/src/tabs/README.md`, `.omp/design/DESIGN.md`.

## Depth 1 -- 30 seconds
- The UI computes no hardware, memory or fit fact. It draws what `/api` says: verdicts come from `/api/capacity` and `/api/plan`, never from a second opinion in the browser (`ui/src/tabs/README.md`).
- The URL is the state. The path names the screen and its subject, the query names the selection. `ui/src/state/routes.ts::parse` and `ui/src/state/routes.ts::href` are the whole scheme and are pure (no React).
- Server strings (planner, fit gate) render through `ui/src/components/Verbatim.tsx::Verbatim` and are never truncated, re-cased or summarised. A missing value is an em dash, never 0.
- No API key is ever rendered, except the one-time reveal. Colours, sizes, space and icons come from the design pack in `.omp/design`, never from a literal in `ui/src`.
- There is no test runner. `*.check.mjs` verifiers are the suite and `cd ui && npm run check` runs them; a green `tsc` proves almost nothing here.

## Depth 2 -- the rules
- **A screen and its subject go in the path; a selection goes in the query.** `?node=`, `?link=`, `?dep=`, `?open=`, `?ctx=`, `?seq=`, `?on=`, `?tp=`/`?pp=`/`?ep=`, `?spec=`, `?win=`. The same node is selectable on three screens and the sheet sits over any of them, so a selection is not owned by a destination. Enforced by `ui/src/state/routes.ts::parse`, pinned by `ui/src/state/router.check.mjs`.
- **A URL has one spelling and survives a round trip.** `parse(href(r))` equals `r` and `href(parse(u))` is stable. Two spellings of one screen make shared links impossible to compare. A model id on a destination with no models is never written. The old `/spend`, `/keys`, `/requests` paths are parse-only aliases of `/usage/...`. Enforced by `ui/src/state/router.check.mjs` walking the whole `ui/src/state/routes.ts::Dest` union, not a hand-kept list.
- **Selecting replaces, opening pushes.** Clicking across the cluster editor is scrubbing; a history entry per click makes Back a hundred-press undo. Opening the sheet or a model pane pushes, closing replaces. Lives in `ui/src/state/router.tsx::RouterProvider`; callers use `ui/src/state/selection.tsx::useSelection` and never touch the URL.
- **`?dep=` carries a served name, never a deployment id.** A deployment id matches nothing, falls through to the default deployment and silently selects someone else's. `ui/src/tabs/models/ServePanel.tsx::ServePanel` navigates with `dep.served_name`; matching is in `ui/src/state/selection.tsx::SelectionProvider`.
- **`?ctx=` and `?seq=` are overrides; absence means "the coordinator picks".** `?ctx=8192` is a different request from no `?ctx=`. Never normalise an explicit default away. `?spec=` is one parameter (method and count are meaningless apart); `?tp`/`?pp`/`?ep` are a set, all three or none, and there is no `?dp=` (`ui/src/state/placement.ts::usePlacement` pairs it to EP).
- **The coordinator answers a deep path with `index.html`, and only for UI paths.** Nothing exists on disk under `/models/<id>`. `control_plane/gateway/app.py::_UIStatics` does this and refuses it for `/api`, `/v1` and missing hashed assets, so a broken deploy is a 404 and not a blank page. Break it and links work once, for the person who never reloads.
- **Pure modules are bundled with `platform: 'neutral'`, so they may not import React or node built-ins.** Logic worth a verifier (`rows.ts`, `routes.ts`, `owner.ts`, `speculative.ts`, `tuning.ts`) lives in a plain `.ts` file beside the component. This is enforcement, not taste: `ui/src/check/harness.mjs::load` fails on a `useState` in the file. Examples: `ui/src/tabs/keys/rows.ts::limitPhase`, `ui/src/tabs/cluster/tuning.ts::tuningState`, `ui/src/tabs/models/speculative.ts::speedup`.
- **Verifiers are discovered, never listed, and a skip is never a pass.** `ui/check.mjs::walk` finds every `*.check.mjs`; each declares needs on a `// requires:` line in its first 4096 bytes (`coordinator`, `python`, `browser`, `fixtures $NAME`, or nothing = hermetic). An unknown word is a FAIL. Skips get their own column; `--strict` makes them failures. The runner refuses to report when passed+failed+skipped does not match what it chose. Read in `ui/check.mjs::requirement`.
- **A coordinator verifier must log in.** A 401 on the probe means it was skipped, not that it passed. Use `ui/src/check/auth.mjs::login` and export `DERATE_CHECK_ADMIN_USER` / `DERATE_CHECK_ADMIN_PASSWORD` (and `DERATE_CHECK_API_KEY` for `/v1`).
- **A verifier asserts at least once.** `ui/src/check/harness.mjs::report` exits 1 when `done()` ran with zero checks, so a loop over an empty list cannot pass.
- **No API key is rendered, and there is no inverse of the scrub.** `ui/src/api/redact.ts::scrub` blanks key-named fields by name and `ui/src/api/redact.ts::DRT_KEY` blanks key-shaped strings by value, in every payload (it mirrors `control_plane/keys/format.py::KEY_PATTERN`). Do not add a reveal control.
- **A key secret is shown exactly once, and only three responses carry it.** `secret_once` on `POST /api/setup/admin`, `POST /api/keys`, `POST /api/keys/{id}/rotate`, fetched through `ui/src/api/client.ts::reqUnscrubbed` and nothing else. It is drawn by `ui/src/components/RevealModal.tsx::RevealModal`, which ignores Escape and the scrim, and the caller drops it on "I saved it". The server keeps only a hash, so there is no second reveal. Pinned by `ui/src/tabs/keys/reveal.check.mjs`.
- **A publisher's mark is subscribed to once per list.** `ui/src/tabs/models/owner.ts::avatarUrl` is synchronous and answers null until its batch lands, so a list that never calls `ui/src/tabs/models/OwnerMark.tsx::useAvatars` sits on monograms forever and reads as a broken feature. `ui/src/tabs/models/OwnerMark.tsx::OwnerMark` must not subscribe itself: hundreds of cards each holding state re-render the grid hundreds of times per batch. `owner.ts` stays React-free for `platform: 'neutral'`.
- **Every dropdown is the themed `Select`, never a native `<select>`.** `ui/src/components/Select.tsx::Select` is the closed choice, `ui/src/components/Combobox.tsx::Combobox` is free text with suggestions. Arrow keys stage, Enter/Space/click commit, Escape is a pure cancel. Enforced by the gate step "no native <select>" in `.omp/scripts/validate/static.mjs::steps`.
- **Server strings render through `Verbatim`; absence is a different picture from zero.** `ui/src/components/Verbatim.tsx::Verbatim` has no truncate or summary path, on purpose. `ui/src/format.ts::fmt` returns an em dash for null/non-finite; `ui/src/components/Bars.tsx::ProportionBar` draws null as a dashed empty track and zero as a solid one. A `stale` `ui/src/components/Readout.tsx::Readout` keeps the last reading rather than zeroing it.
- **Colour reports state, never category, and is never the only carrier.** `ui/src/components/Lamp.tsx::Lamp` carries `label`, and `hollow` means "not hearing from the source". If something is coloured, something is true.
- **Design pack first, then app code.** Tokens in `.omp/design/tokens/tokens.json` (then `npm run tokens:build`; `tokens.css` is generated, never edit it), icons in `.omp/design/icons/svg/*.svg` + `icons.json` drawn to `.omp/design/icons/README.md`, components in `.omp/design/components/registry.json` plus a spec in `.omp/design/DESIGN.md`. Search the pack before adding any of them. An icon reaches the screen only through `ui/src/components/Icon.tsx::Icon` (no inline `<svg>`; `label` or `decorative` is a required choice). Enforced by `.omp/scripts/validate/static.mjs::steps` (literal colours, inline svg) and `cd .omp && npm run check`.
- **A new fg/bg colour pair needs a row in `.omp/design/tokens/pairs.json`.** `npm run tokens:check` is the contrast gate and the staleness check. Full list of pack rules: `.omp/RULES.md`.
- **Live and screen agree with each other; the gate decides whether it shipped.** `ui/src/shell/screens.check.mjs` (`requires: browser coordinator`) drives the cached Chromium over every `DESTINATIONS` entry and fails on a console error, a non-2xx from the coordinator, an empty `#root`, a misaligned row (`ui/src/check/alignment.mjs::alignmentAudit`) or a key on screen. `npm run screens` writes `ui/screens/<dest>.png`; open them.

## Depth 3 -- how to change it
**Add a screen or a pane**
1. Add the member to `ui/src/state/routes.ts::Dest` and its path to `SEGMENT` (so `ui/src/state/routes.ts::DESTINATIONS` follows). A pane of `/usage` is a `USAGE_SUBS` entry instead.
2. Teach `ui/src/state/routes.ts::parse` and `ui/src/state/routes.ts::href` the new query keys; keep one spelling per meaning.
3. Mount it in `ui/src/shell/AppShell.tsx::AppShell`, hidden with `hidden` and not unmounted, so polls and scroll survive a visit elsewhere (only setup and login are early returns).
4. Add cases to `ui/src/state/router.check.mjs`, then `cd ui && npm run screens` and look at the PNG. Add the screen to the folder's README table.

**Add a control that shows one number and applies another** (verdict-linked controls)
1. Put the decision (what to send, what to show) in a pure `.ts` beside the component, as `ui/src/tabs/models/speculative.ts` does.
2. The displayed value and the value sent must be one value; derive both from the same function.
3. Add `<name>.check.mjs` starting with `load(import.meta.url, './<name>.ts')` and `report()`; pin both observed edge pairs.

**Add a polled resource or a client call**
1. Add the response type to `ui/src/api/types.ts` (mirror the contract, do not invent), the method to `ui/src/api/client.ts::Backend` and its implementation with `ui/src/api/client.ts::req`.
2. Use `ui/src/state/backend.tsx::useResource` once per screen and pass the result down as a prop; use `useKeyedResource` when the read takes an argument (a plain one keeps showing the old subject's data).
3. Never use `reqUnscrubbed` for anything but the three one-time-secret responses.

**Add a themed component, icon or token**
1. Search `.omp/design/components/registry.json`, `icons.json`, `tokens.json` first. Reuse a match.
2. Component: file in `ui/src/components/`, a registry row (kind `shared`; `screen` for a tab-local one), a spec under `.omp/design/DESIGN.md` Components, only `var(--token)` and `Icon`.
3. Icon: `svg/<name>.svg` + `icons.json`, then `npm run icons:build && npm run icons:lint`. Colour: token with `$description`, a `pairs.json` row, then `npm run tokens:build && npm run tokens:check`.
4. `cd .omp && npm run check`; the gate also checks README file tables and line budgets.

**Add a list that shows publisher marks**
1. Call `ui/src/tabs/models/OwnerMark.tsx::useAvatars` once in the list component, render `OwnerMark` per row.
2. Pass `reserve` on a row list so a publisher-less row keeps its column. Do not subscribe inside the row.

**Add a verifier**
1. Name it `*.check.mjs` beside the module; open with `// requires:` only if it truly needs a coordinator, browser, python or fixtures (hermetic is the safe default). Never `process.exit(0)` on a missing dependency.
2. Run alone with `cd ui && node src/<path>.check.mjs`, then `npm run check -- --strict`; it must land in the PASS column, not skipped.

## Pitfalls
- **Key-shaped strings have no field name to catch them by.** A log line, an error message or a request row can quote a `drt_` key, so the scrub works by value as well as by name (`ui/src/api/redact.ts::scrub`). A new response field that carries a credential needs its name or its value shape added there; `ui/src/shell/screens.check.mjs` primes the server's own `Redactor` with the real stored values and fails if any is on screen.
- **`api_key_ref` rendered as `***`.** `redact.ts` once tested references against a stricter pattern than the server and blanked a correct `my-openrouter-key`. Use `ui/src/api/keyshape.ts::looksLikeRefName`, a port of the server's rule; `keyfield.check.mjs` runs the Python to catch drift that `tsc` cannot see.
- **Bare `npx tsc --noEmit` typechecks nothing** (`ui/tsconfig.json` has `"files": []`). Use `cd ui && npm run typecheck` or `npx tsc -b --force`. `npx vite build` bundles without typechecking, which tells your breakage from a peer's.
- **A shared checkout changes under you.** Peers edit `ui/` at once; `npm run build` can fail on a type error in a file you never touched. Grep for a marker unique to your edit before declaring done.
- **A coordinator verifier that exits 0 when the coordinator is unreachable** made "checked nothing" equal "checked everything". The `requires:` line is how it is declared now; a 401 (no login) also reads as unreachable, so log in.
- **A hue pair that passes every assertion and cannot be told apart.** The chat model tags shipped blue against teal, 29 degrees apart, with all checks green. `ui/src/tabs/chat/tags.check.mjs` now pins at least 40 degrees between hues and from live/warn/fault. Look at the screenshot, not just the assertion.
- **A flex row that wraps misaligns.** A `.chips` group with a bottom margin inside a centred row made `/usage/requests` bounce. The alignment audit measures at 1440, 1024 and 390 px because wrapping is what breaks it.
- **A deep link that only works while you click to it.** If `_UIStatics` stops answering UI paths with the document, every link works until someone reloads. Test with a reload, not by navigation.
- **The live UI is `ui/dist`, and rebuilding it is live on :8088.** `npx vite build` goes live with no restart only if `DERATE_UI_DIR` still points at `ui/dist`; equal asset hashes (`curl -s localhost:8088/ | grep -o 'assets/index-[^"]*\.js'`) are the only proof. Never start a second process on the live data dir; boot scratch instances on 18xxx.
- **Client mirrors of server predicates drift silently.** `ui/src/state/runtime.ts::canCarryRank` mirrors the placement refusal; the node board must keep agreeing with it or it offers a tick the launch answers with 400. Change both or neither.
- **A new control hides because its state unmounted.** A control inside a card that its own `result` builds unmounts the instant it nulls that result; keep a mode in local state, not in a value the control itself clears.
- **A README table that misses the new file.** The gate checks README file tables and line budgets (`.omp/line-budget.json`); a file crossing its budget inside the change that grew it passes any later ratchet.

## Functions
- `ui/src/state/routes.ts::parse` -- URL string to a `Route`; pure. Use in tests and anywhere a path is read.
- `ui/src/state/routes.ts::href` -- `Route` to its one canonical URL. Never hand-build a path.
- `ui/src/state/routes.ts::DESTINATIONS` -- every destination, derived from `SEGMENT`; walk this, never a list.
- `ui/src/state/router.tsx::useRouter` -- current route plus `navigate(partial, {replace})`.
- `ui/src/state/router.tsx::plainClick` -- true for an unmodified left click, so links keep ctrl/cmd-click.
- `ui/src/state/selection.tsx::useSelection` -- node, link, deployment and sheet read/written through the URL.
- `ui/src/state/backend.tsx::useResource` -- poll one endpoint, keep the last good value through a failure.
- `ui/src/state/backend.tsx::useKeyedResource` -- same, for a parameterised read; clears data when the key changes.
- `ui/src/api/client.ts::req` -- the scrubbed JSON request every `Backend` method uses.
- `ui/src/api/client.ts::reqUnscrubbed` -- the unscrubbed request for the three one-time-secret responses only.
- `ui/src/api/redact.ts::scrub` -- recursive credential scrub applied on the way in.
- `ui/src/api/keyshape.ts::looksLikeRefName` -- is this a reference name or key material (port of the server rule).
- `ui/src/components/Verbatim.tsx::Verbatim` -- render a server sentence exactly as received.
- `ui/src/components/Verbatim.tsx::VerbatimList` -- the planner's rejected list, one line each.
- `ui/src/components/Select.tsx::Select` -- the closed-choice dropdown; never a native `<select>`.
- `ui/src/components/Combobox.tsx::Combobox` -- free text with filtered suggestions.
- `ui/src/components/listNav.ts::moveActive` -- pure index arithmetic Select and Combobox share.
- `ui/src/components/useDismiss.ts::useDismiss` -- outside-click, Escape and blur dismissal for popups.
- `ui/src/components/focusTrap.ts::containTab` -- keep Tab inside a modal card; use in every dialog.
- `ui/src/components/RevealModal.tsx::RevealModal` -- the one place a key secret is drawn.
- `ui/src/components/Icon.tsx::Icon` -- the only way an icon reaches the screen.
- `ui/src/components/Lamp.tsx::Lamp` -- status lamp: live/warn/fault/idle, hollow when the source is quiet.
- `ui/src/components/Readout.tsx::Readout` -- fixed-width mono number that keeps the last reading when stale.
- `ui/src/components/Bars.tsx::ProportionBar` -- bar that draws null and zero differently.
- `ui/src/components/Panel.tsx::Section` -- sidebar section with a hairline, no gaps or shadows.
- `ui/src/format.ts::fmt` -- number to text; em dash for null.
- `ui/src/tabs/keys/rows.ts::EM` -- the em dash constant for an absent value.
- `ui/src/tabs/models/OwnerMark.tsx::useAvatars` -- subscribe a list to avatar batches, once per list.
- `ui/src/check/harness.mjs::load` -- esbuild a TS module (`platform: 'neutral'`) so a verifier can import it.
- `ui/src/check/harness.mjs::report` -- `check`, `note`, `done`; non-zero on a failure or on nothing checked.
- `ui/src/check/browser.mjs::findBrowser` -- the Chromium already on the machine, or why there is none; never downloads.
- `ui/src/check/alignment.mjs::alignmentAudit` -- in-page audit that a flex row's controls share a centre line.
