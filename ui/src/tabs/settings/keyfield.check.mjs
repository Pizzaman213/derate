// requires: python -- reads looks_like_secret out of the server's own module
// Verifier for keyfield.ts. There is no UI test runner here, so the rule is
// the same as rows.check.mjs and layout.check.mjs: esbuild-bundle the module
// and exercise it, because a green tsc says nothing about what the predicate
// answers.
//
// What makes this one worth having: keyfield.ts is a port of
// control_plane/providers/secrets.py `looks_like_secret`, and a port drifts.
// The drift is invisible to types on both sides and shows up as a form that
// warns about input the server accepts, or stays quiet about input it refuses.
// So the expectations here are not restated by hand -- they are computed by
// running the Python, which is the only way the check can still fail after
// somebody edits the Python and not the TypeScript.
//
// The same drift already shipped once: redact.ts tested a reference against
// /^[A-Z][A-Z0-9_]{0,63}$/ while the server accepted `my-openrouter-key`, so a
// correctly-configured name rendered on screen as `***`.
//
//   node ui/src/tabs/settings/keyfield.check.mjs
import { execFileSync } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const repo = join(here, '..', '..', '..', '..')
const out = mkdtempSync(join(tmpdir(), 'keyfield-'))
const bundle = join(out, 'bundle.mjs')
execFileSync(
  'npx',
  ['esbuild', join(here, 'keyfield.ts'), '--bundle', '--format=esm', `--outfile=${bundle}`],
  { stdio: 'pipe', cwd: join(here, '..', '..', '..') },
)
const {
  looksLikeSecret,
  looksLikeRefName,
  mintedRef,
  predictedProviderId,
  keyFieldWarning,
  keyPlaceholder,
  keyStateNote,
  keyStateTone,
} = await import(pathToFileURL(bundle).href)

let failures = 0
const check = (name, actual, expected) => {
  const ok = actual === expected
  if (!ok) failures++
  console.log(
    `${ok ? 'ok  ' : 'FAIL'} ${name}${ok ? '' : `\n       got ${JSON.stringify(actual)}\n       want ${JSON.stringify(expected)}`}`,
  )
}

// Every literal from tests/test_providers.py's looks_like_secret table, plus
// the shapes the two modes have to tell apart, plus the references derate
// mints for itself -- which must read as names, or the UI hides its own.
const CASES = [
  'OPENROUTER_API_KEY',
  'my-openrouter-key',
  'openrouter_key',
  'https://openrouter.ai/api/v1',
  'REAL_KEY',
  'sk-or-v1-0123456789abcdef0123456789abcdef',
  'sk-ant-api03-abcdefghij0123456789klmnopqrst',
  'sk-proj-abcdefghij0123456789klmnopqrstuvwx',
  'gsk_abcdefghij0123456789klmnopqrstuvwx',
  'hf_abcdefghij0123456789klmnopqrstuv',
  'AIzaSyA0123456789abcdefghijklmnopqrstuv',
  'Bearer abcdefghij0123456789klmnopqrst',
  'api_key=abcdefghij0123456789klmnopqrst',
  'https://example.com/v1?api_key=abcdefghij0123456789klmnop',
  'DERATE_OPENROUTER_API_KEY',
  'DERATE_LAN_OLLAMA_2_API_KEY',
  '',
  'a',
]

// The expectations, straight out of the module under port. Nothing is asserted
// here that the server does not itself answer.
const python = JSON.parse(
  execFileSync(
    'python3',
    [
      '-c',
      [
        'import inspect, json, re, sys',
        'from control_plane.providers.secrets import looks_like_secret',
        'from control_plane.providers.service import ProviderService, minted_ref',
        'from control_plane.providers.serialization import kinds_public',
        'cases = json.load(sys.stdin)',
        'status = inspect.getsource(ProviderService.key_status)',
        'print(json.dumps({',
        '  "secret": [looks_like_secret(c) for c in cases],',
        '  "minted": {i: minted_ref(i) for i in ["openrouter", "lan-ollama-2", "x"*90, "", "a.b_c"]},',
        '  "key_states": sorted(set(re.findall(r\'"key_state": "([a-z_]+)"\', status))),',
        '  "key_sources": sorted(set(re.findall(r\'"key_source": "([a-z.]+)"\', status))),',
        '  "key_kinds": [k["kind"] for k in kinds_public() if k["requires_key"]],',
        '}))',
      ].join('\n'),
    ],
    { cwd: repo, input: JSON.stringify(CASES), encoding: 'utf8' },
  ),
)

CASES.forEach((value, i) => {
  const label = value === '' ? '(empty)' : value.length > 34 ? `${value.slice(0, 31)}…` : value
  check(`looksLikeSecret agrees with Python: ${label}`, looksLikeSecret(value), python.secret[i])
})

// The minted name is the one reference derate creates itself. If the two sides
// spell it differently the form promises a name the coordinator never writes.
for (const [providerId, expected] of Object.entries(python.minted)) {
  const label = providerId.length > 20 ? `${providerId.slice(0, 17)}…` : providerId || '(empty)'
  check(`mintedRef agrees with Python: ${label}`, mintedRef(providerId), expected)
}

// Invariant 5 of the plan: a minted name the UI would render as *** is derate
// hiding its own reference from the operator who needs to read it.
for (const providerId of ['openrouter', 'lan-ollama-2', 'x'.repeat(90), 'a.b_c']) {
  check(`minted name displays as a name: ${providerId.slice(0, 17)}`, looksLikeRefName(mintedRef(providerId)), true)
  check(`minted name is within 64 chars: ${providerId.slice(0, 17)}`, mintedRef(providerId).length <= 64, true)
}

// The divergence this module was written to end: the server accepts these, so
// the UI must display them rather than mask them as key material.
check('a lowercase hyphenated ref is a name', looksLikeRefName('my-openrouter-key'), true)
check('an env var name is a name', looksLikeRefName('OPENROUTER_API_KEY'), true)
check('an empty ref is a name', looksLikeRefName(''), true)
check('a pasted key is not a name', looksLikeRefName('sk-or-v1-0123456789abcdef0123456789abcdef'), false)
check('a non-string is not a name', looksLikeRefName(null), false)

// _mint_id, mirrored: the kind, then the first free -N.
check('first provider of a kind takes the bare id', predictedProviderId('openrouter', []), 'openrouter')
check('a taken id steps to -2', predictedProviderId('openrouter', ['openrouter']), 'openrouter-2')
check('and keeps stepping', predictedProviderId('openrouter', ['openrouter', 'openrouter-2']), 'openrouter-3')

// Warnings: each mode catches the other's input, and neither fires on its own.
check('ref mode warns on a pasted key',
  keyFieldWarning('ref', 'sk-or-v1-0123456789abcdef0123456789abcdef') !== null, true)
check('ref mode is quiet on a name', keyFieldWarning('ref', 'OPENROUTER_API_KEY'), null)
check('key mode warns on a typed name',
  keyFieldWarning('key', 'OPENROUTER_API_KEY') !== null, true)
check('key mode is quiet on a key',
  keyFieldWarning('key', 'sk-or-v1-0123456789abcdef0123456789abcdef'), null)
check('neither mode warns on an empty field', keyFieldWarning('key', '   '), null)

// Key state: the only thing about a key that ever reaches a screen, so the
// vocabulary is read out of the server's own key_status rather than restated
// here. A state added there and not described here would render as "state
// unknown", which is the worst kind of failure -- it looks like an answer.
const unknown = keyStateNote(null, null)
check('a state the port cannot report says so', unknown, 'state unknown')
check('and carries no colour', keyStateTone(null), 'muted')
for (const state of python.key_states) {
  check(`keyStateNote describes the server's "${state}"`, keyStateNote(state, null) !== unknown, true)
}
for (const source of python.key_sources) {
  check(
    `keyStateNote names the server's "${source}"`,
    keyStateNote('set', source).includes(source),
    true,
  )
}
// The paste field's own hint. The kinds are the server's, so a kind added
// there arrives here without this file being touched -- which is the point,
// because the failure it guards is silent: a hint that reads as the name of an
// environment variable is the exact confusion the two-mode field was built to
// end, printed by the field itself. `completed` stands in for what an operator
// would actually paste, since a placeholder is only ever a prefix.
const completed = (hint) => hint.replace('…', 'abcdefghij0123456789klmn')
for (const kind of python.key_kinds) {
  const hint = keyPlaceholder(kind)
  check(`${kind} has a key hint`, hint.length > 0, true)
  check(
    `${kind}'s hint would not trip the field's own warning`,
    keyFieldWarning('key', completed(hint)),
    null,
  )
  // looksLikeSecret is the server's screen, ported; the block at the top of
  // this file is what establishes the two still agree. So a hint that passes
  // here is one the coordinator would also read as a key.
  check(`${kind}'s hint reads as key material`, looksLikeSecret(completed(hint)), true)
}

// Named rather than derived, because it is the requirement and not a port: the
// freeze point this field removes was an operator holding an OpenRouter key
// with nowhere obvious to put it. A refactor that drops the case falls back to
// the generic hint silently, and the field stops naming what it wants.
check(
  'OpenRouter is offered its own key shape, not the generic one',
  keyPlaceholder('openrouter').startsWith('sk-or-v1-'),
  true,
)
check('an unrecognised kind falls back rather than guessing', keyPlaceholder('nope'), 'sk-…')

check('a key that resolves reads as live', keyStateTone('set'), 'ok')
check('a reference that does not resolve warns rather than faults', keyStateTone('missing'), 'warn')
check('a kind that needs no key is neither', keyStateTone('not_needed'), 'muted')

rmSync(out, { recursive: true, force: true })
console.log(failures ? `\n${failures} failed` : '\nall passed')
if (failures) process.exit(1)
