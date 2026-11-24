// What a verifier needs, read off its `// requires:` line. `ui/check.mjs` probes
// each one; this file only parses, so `requires.check.mjs` can pin the grammar.
//
// Every word before a `--` is a requirement (`browser coordinator` is two), and
// `fixtures` takes the next word as its variable. Until 2026-11-24 the line was
// read as one kind plus an argument, so the coordinator in `browser coordinator`
// was never probed and, with no login, those verifiers ran and failed instead
// of skipping.
//
// No line means hermetic -- needs nothing but this checkout -- which is the
// safe default: a verifier that forgets to declare a dependency FAILS loudly on
// a machine without it, rather than being silently skipped everywhere forever.
// A line that is there but names nothing, or a `fixtures` with no variable,
// becomes a word the runner does not know, and an unknown word is fatal.

/** @param {string} head the top of a verifier's source */
export function parseRequires(head) {
  const m = head.match(/^\/\/ requires:(.*)$/m)
  if (!m) return [{ kind: 'hermetic' }]
  const all = m[1].trim().split(/\s+/).filter(Boolean)
  const words = all.includes('--') ? all.slice(0, all.indexOf('--')) : all
  const reqs = []
  for (let i = 0; i < words.length; i++) {
    if (words[i] !== 'fixtures') reqs.push({ kind: words[i] })
    else if (words[i + 1]) reqs.push({ kind: 'fixtures', arg: words[++i] })
    else reqs.push({ kind: 'fixtures with no variable named' })
  }
  return reqs.length ? reqs : [{ kind: 'an empty requires line' }]
}
