// The `// requires:` grammar the runner skips and fails by. Hermetic.
//
// The bug this pins: `browser coordinator` was read as one kind plus an
// argument, so the coordinator was never probed and four verifiers failed with
// no login instead of skipping. Every case below is a line form that exists in
// the tree or a malformed one that must stay loud.
import { parseRequires } from './requires.mjs'
import { report } from './harness.mjs'

const { check, done } = report()
const kinds = (head) => JSON.stringify(parseRequires(head))
const is = (head, want, why) => check(kinds(head) === JSON.stringify(want), `${why}: ${kinds(head)}`)

is('import x from "y"\n', [{ kind: 'hermetic' }], 'no line is hermetic')
is('// requires: coordinator -- reads /api\n', [{ kind: 'coordinator' }], 'one word, prose after -- ignored')
is('// requires: browser coordinator -- a real browser\n', [{ kind: 'browser' }, { kind: 'coordinator' }],
  'two words are two requirements, the coordinator is probed')
is('// requires: browser coordinator python -- x\n', [{ kind: 'browser' }, { kind: 'coordinator' }, { kind: 'python' }],
  'three words, three requirements')
is('// requires: python\n', [{ kind: 'python' }], 'no prose at all')
is('// requires: fixtures FOO coordinator\n', [{ kind: 'fixtures', arg: 'FOO' }, { kind: 'coordinator' }],
  'fixtures takes the next word and only that one')
is('// requires:\tbrowser\tcoordinator\n', [{ kind: 'browser' }, { kind: 'coordinator' }], 'tabs separate words')
// Malformed lines stay loud: each becomes a word the runner does not know, which is fatal.
is('// requires: fixtures\n', [{ kind: 'fixtures with no variable named' }], 'a bare fixtures is not a skip')
is('// requires: fixtures --\n', [{ kind: 'fixtures with no variable named' }], 'nor is fixtures followed by --')
is('// requires:\n', [{ kind: 'an empty requires line' }], 'an empty line is not hermetic')
is('// requires: -- because\n', [{ kind: 'an empty requires line' }], 'nor is prose with no words')
is('// requires: bogus\n', [{ kind: 'bogus' }], 'an unknown word reaches the runner as itself')
done()
