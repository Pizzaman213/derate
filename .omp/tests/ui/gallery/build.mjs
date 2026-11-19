// Bundle the gallery into tests/ui/.build/ (gitignored) with esbuild.
//
//   node tests/ui/gallery/build.mjs      (the browser checks call build() themselves)
//
// absWorkingDir is ui/ so `react`, `@fontsource/*` and friends resolve from the
// UI's node_modules -- the only copy of React in the repo. Fonts are emitted
// as files beside the bundle so the gallery renders in Plex like the product.

import { build } from 'esbuild'
import { mkdirSync, writeFileSync, existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { caseSensitiveRelative, EXTENSIONS } from '../lib/esbuild.mjs'

const here = dirname(fileURLToPath(import.meta.url))
export const OUT = resolve(here, '..', '.build')
const ui = resolve(here, '..', '..', '..', '..', 'ui')

export async function buildGallery() {
  mkdirSync(OUT, { recursive: true })
  // One React. `.omp/node_modules` carries its own copy (a dependency of the
  // omp types package), and a gallery entry under `.omp/` would resolve it
  // while the components under `ui/src` resolve the UI's -- two dispatchers,
  // "Invalid hook call". Every react specifier is pinned to the UI's.
  const uiMod = (m) => join(ui, 'node_modules', m)
  await build({
    entryPoints: [join(here, 'main.tsx')],
    bundle: true,
    format: 'esm',
    outdir: OUT,
    entryNames: 'gallery',
    assetNames: 'assets/[name]-[hash]',
    absWorkingDir: ui,
    nodePaths: [join(ui, 'node_modules')],
    alias: { react: uiMod('react'), 'react-dom': uiMod('react-dom'), 'react/jsx-runtime': uiMod('react/jsx-runtime'), 'react-dom/client': uiMod('react-dom/client') },
    jsx: 'automatic',
    platform: 'browser',
    target: 'es2022',
    resolveExtensions: EXTENSIONS,
    plugins: [caseSensitiveRelative],
    loader: { '.woff2': 'file', '.woff': 'file', '.json': 'json' },
    define: { 'process.env.NODE_ENV': '"development"' },
    logLevel: 'warning',
  })
  writeFileSync(join(OUT, 'index.html'), `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>derate — gallery</title><link rel="stylesheet" href="gallery.css"></head><body><div id="root"></div><script type="module" src="gallery.js"></script></body></html>\n`)
  return OUT
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  await buildGallery()
  console.log(`gallery: built into ${OUT}`)
}
