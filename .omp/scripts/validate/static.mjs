// Pattern checks the design pack and the UI guardrails ask for, run on source text.
// They look at CHANGED files only: the tree has older code that predates a rule, and a gate
// that is red on day one gets ignored. Touch a file and it has to meet the rule.
import { changed, read } from './context.mjs'

const scan = (files, test) => files.flatMap((p) => read(p).flatMap((l, i) => (test(l, p) ? [`${p}:${i + 1}  ${l.trim().slice(0, 90)}`] : [])))
const isUi = (p) => /^ui\/src\/.*\.(tsx?|css)$/.test(p)

export const steps = [
  { name: 'no native <select> (use components/Select)', run: () =>
    scan(changed.filter((p) => p.startsWith('ui/src/') && p.endsWith('.tsx') && !p.endsWith('components/Select.tsx')),
      (l) => /(^|[^`\w])<select[\s>]/.test(l) && !/^\s*(\/\/|\*|\/\*)/.test(l)) },
  { name: 'no literal colours in changed UI files (use tokens)', run: () =>
    scan(changed.filter((p) => isUi(p) && !p.endsWith('.css')),
      (l) => /['"`]#[0-9a-fA-F]{3,8}['"`]|\b(rgba?|hsla?)\(/.test(l) && !/design-pack:/.test(l)) },
  { name: 'no inline <svg> outside Icon.tsx in changed UI files', run: () =>
    changed.filter((p) => p.endsWith('.tsx') && !p.endsWith('components/Icon.tsx') && !read(p).some((l) => /design-pack: data-svg/.test(l)))
      .flatMap((p) => scan([p], (l) => /<svg[\s>]/.test(l))) },
  { name: 'a changed contract, route or key/limit module has a changed test', run: () => {
    const risky = changed.filter((p) => /^control_plane\/(contracts|keys|limits|auth)\/.*\.py$/.test(p) || /^control_plane\/gateway\/\w*_api\.py$/.test(p) || p === 'control_plane/gateway/authz.py')
    const tests = changed.filter((p) => /^tests\/unit\/.*\.py$/.test(p))
    return risky.length && !tests.length ? [`changed ${risky.join(', ')} but no file under tests/unit/ changed`] : []
  } },
]
