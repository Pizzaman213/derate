// What every check shares: the repo root, the base the change is measured from,
// which files changed, and a line reader that counts lines the way `wc -l` does.
import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

export const git = (...a) => execFileSync('git', a, { encoding: 'utf8', maxBuffer: 1 << 28 }).trim()

// From this file's location, not `git rev-parse`: under a git hook GIT_DIR is set, and from
// `npm --prefix .omp` the cwd is .omp, which git would then treat as the work tree root.
export const root = fileURLToPath(new URL('../../../', import.meta.url)).replace(/\/$/, '')
process.chdir(root)

export const baseRef = process.env.VALIDATE_BASE || 'dev'
export const base = (() => {
  try { return git('merge-base', 'HEAD', baseRef) } catch {
    console.error(`No merge-base between HEAD and '${baseRef}'. Fetch it, or set VALIDATE_BASE=<ref>.`)
    process.exit(1)
  }
})()

export const allow = existsSync('.omp/validate.allow.json')
  ? JSON.parse(readFileSync('.omp/validate.allow.json', 'utf8')) : {}
export const allowedPath = (p) => (allow.paths ?? []).some((x) => p.startsWith(x))

const lines = (s) => s.split('\n').filter((l, i, a) => i < a.length - 1 || l !== '')
export const read = (p) => lines(readFileSync(p, 'utf8'))
export const lineCount = (text) => lines(text).length

export const untracked = git('ls-files', '-o', '--exclude-standard').split('\n').filter(Boolean)
const tracked = git('diff', '--name-only', '--diff-filter=ACMR', base).split('\n').filter(Boolean)
export const changed = [...new Set([...tracked, ...untracked])].filter((p) => existsSync(p) && !allowedPath(p))
export const allFiles = () => [...new Set([...git('ls-files').split('\n'), ...untracked])].filter((p) => p && existsSync(p))
