// A case-sensitive resolver for relative imports.
//
// esbuild matches directory entries case-insensitively, and ui/src has
// `tabs/dashboard/Chart.tsx` beside `chart.ts`: `./chart` resolved to the
// component and `./Chart` to the module, each the wrong one. This plugin
// resolves `./x` and `../x` by trying the exact-case candidates with
// fs.existsSync (case-sensitive on Linux) and lets esbuild handle the rest.

import { existsSync, statSync } from 'node:fs'
import { dirname, resolve } from 'node:path'

export const EXTENSIONS = ['.ts', '.tsx', '.mjs', '.js', '.css', '.json']

export const caseSensitiveRelative = {
  name: 'case-sensitive-relative',
  setup(build) {
    build.onResolve({ filter: /^\.\.?\// }, (args) => {
      const base = resolve(args.resolveDir || dirname(args.importer), args.path)
      const candidates = [base, ...EXTENSIONS.map((e) => base + e), ...EXTENSIONS.map((e) => resolve(base, 'index' + e))]
      for (const c of candidates) if (existsSync(c) && statSync(c).isFile()) return { path: c }
      return undefined
    })
  },
}
