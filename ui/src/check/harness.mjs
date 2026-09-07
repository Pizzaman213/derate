// The two things every verifier in this repo was writing by hand.
//
// There is no test runner in `ui/` -- the `*.check.mjs` files ARE the test
// suite, each one written because a class of bug is invisible to types. That
// pattern works, and the reason there are not more of them is that the first
// thirty lines of a new verifier were always the same two chores: bundle a TS
// module so node can import it, and count failures so the exit code means
// something. Fifteen files had hand-rolled copies, in two different idioms.
//
// So a new verifier now starts at its first assertion:
//
//   import { load, report } from '../../check/harness.mjs'
//   const M = await load(import.meta.url, './rows.ts')
//   const { check, done } = report()
//   check(M.rows([]).length === 0, 'an empty registry has no rows')
//   done()
//
// Nothing here knows about the runner (`ui/check.mjs`). A verifier stays a
// program you can run on its own -- `node src/tabs/models/rows.check.mjs` --
// because that is the inner loop, and a harness that only worked under a
// runner would have taken that away.

import { build } from 'esbuild'
import { fileURLToPath } from 'node:url'

/** Bundle a TypeScript module and return it, resolved relative to the caller.
 *
 *  esbuild rather than a direct import because node's ESM resolver will not
 *  resolve the extensionless specifiers these modules use for each other
 *  (`from '../../api/types'`), and the `write: false` + data-URI form rather
 *  than a temp file because it leaves nothing on disk to clean up.
 *
 *  `platform: 'neutral'` keeps this honest: a module that reached for a node
 *  built-in would fail here rather than passing a check it could never pass in
 *  a browser. */
export async function load(callerUrl, rel) {
  const out = await build({
    entryPoints: [fileURLToPath(new URL(rel, callerUrl))],
    bundle: true,
    write: false,
    format: 'esm',
    platform: 'neutral',
    logLevel: 'silent',
  })
  const js = out.outputFiles[0].text
  return import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'))
}

/** PASS/FAIL printing, failure counting, and the exit code.
 *
 *  `done()` exits non-zero if anything failed AND if nothing was checked at
 *  all. A verifier whose assertions were all skipped past by a `for` loop over
 *  an empty list is the same lie as a green gate that ran nothing, and it is a
 *  lie this suite has told before. */
export function report() {
  let failures = 0
  let total = 0

  const check = (ok, what) => {
    total += 1
    if (!ok) failures += 1
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${what}`)
    return ok
  }

  // Context that is not an assertion: a measured number worth printing, a
  // caveat about what the live data happened to contain. Never counted.
  const note = (what) => console.log(`      ${what}`)

  const done = () => {
    if (total === 0) {
      console.log('\nno checks ran -- a verifier that asserts nothing is not a pass')
      process.exit(1)
    }
    console.log(failures === 0
      ? `\nall ${total} checks passed`
      : `\n${failures} of ${total} checks FAILED`)
    process.exit(failures === 0 ? 0 : 1)
  }

  return { check, note, done }
}
