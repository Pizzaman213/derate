// A new top-level function whose name is already defined in another file is usually the
// same job twice. Methods and route handlers (indented) repeat names on purpose and are
// the duplication-reviewer's judgement call, not this check's.
import { spawnSync } from 'node:child_process'
import { allow, allowedPath, base, changed, git, read, untracked } from './context.mjs'

const SKIP = new Set(['main', 'create_router', 'create_app', 'setup', 'build', 'run', 'get', 'post', 'patch', 'delete', 'list', 'default', 'open', 'close', 'check', 'load', 'report', 'req', 'seg', 'qs', ...(allow['duplicate-names'] ?? [])])
const DEF = /^(?:async\s+def|def)\s+([A-Za-z_]\w+)\(|^export\s+(?:async\s+)?function\s+(\w+)|^export\s+const\s+(\w+)\s*=|^(?:async\s+)?function\s+(\w+)\(/
const GREP = (name) => `^(async +def|def|export +(async +)?function|export +const|function|const) +${name}\\b`

/** [{ text, n }] for the lines a file added: all of it when new, else the diff's `+` lines. */
const addedLines = (p) => {
  if (untracked.includes(p)) return read(p).map((text, i) => ({ text, n: i + 1 }))
  const out = []
  let n = 0
  for (const l of git('diff', '-U0', base, '--', p).split('\n')) {
    const hunk = l.match(/^@@ .*\+(\d+)/)
    if (hunk) n = Number(hunk[1]) - 1
    else if (l.startsWith('+') && !l.startsWith('+++')) out.push({ text: l.slice(1), n: ++n })
  }
  return out
}

const addedNames = () => changed
  .filter((p) => /\.(py|tsx?|mjs)$/.test(p) && !/(^|\/)(tests?|node_modules)\//.test(p) && !p.endsWith('.check.mjs'))
  .flatMap((file) => addedLines(file).flatMap(({ text, n }) => {
    const m = text.match(DEF)
    const name = m && (m[1] || m[2] || m[3] || m[4])
    return name && !/^(__|test_)/.test(name) && !SKIP.has(name) ? [{ name, file, n }] : []
  }))

const definedElsewhere = (name, file) => {
  const args = ['grep', '-nwE', GREP(name), '--untracked', '--', '*.py', '*.ts', '*.tsx', '*.mjs', ':!tests', ':!*.check.mjs', `:!${file}`]
  return spawnSync('git', args, { encoding: 'utf8' }).stdout.trim().split('\n').filter(Boolean)
    .filter((l) => !allowedPath(l.split(':')[0]))
}

export const steps = [
  { name: 'new functions do not already exist in another file', run: () =>
    addedNames().flatMap(({ name, file, n }) => {
      const other = definedElsewhere(name, file)
      return other.length ? [`${file}:${n}  \`${name}\` is also defined at ${other[0].split(':').slice(0, 2).join(':')}${other.length > 1 ? ` (+${other.length - 1})` : ''}`] : []
    }) },
]
