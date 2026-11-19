#!/usr/bin/env node
// Build or refresh the code graph and merge the design graph into it.
//
//   cd .omp && npm run graph:build     # --full: extract . --code-only --force (GRAPHIFY_FORCE=1)
//   cd .omp && npm run graph:update    # extract . --code-only, incremental via graphify's manifest cache
//
// Both paths are `extract --code-only`, never `graphify update`: update also
// indexes every markdown heading as a node (1,027 of them here) and the two
// commands would otherwise disagree about what the graph contains. --force
// re-scans everything and lets the graph shrink after a refactor that deleted
// code; without it graphify refuses to overwrite a graph with fewer nodes and
// says so.
//
// Output layout (all under .omp/graph/):
//   graphify-out/        graphify's own directory (GRAPHIFY_OUT); gitignored.
//                        Its basename is already in graphify's skip set, so no
//                        source directory is excluded by accident.
//   design-graph.json    the design nodes and edges alone (design-graph.mjs)
//   graph.json           code + design, the graph every query reads
//   god-nodes.json       `graphify god-nodes --json`
//   GRAPH_REPORT.md      graphify's community report (cluster-only, no LLM)
// Everything stays under .omp/graph; nothing is written at the repo root.
// PYTHONHASHSEED=0 keeps the clustering deterministic.

import { spawnSync } from 'node:child_process'
import { existsSync, writeFileSync, copyFileSync } from 'node:fs'
import { dirname, join, resolve, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const repo = resolve(here, '..', '..')
const out = join(here, 'graphify-out')
const full = process.argv.includes('--full') || !existsSync(join(out, 'graph.json'))
const env = { ...process.env, GRAPHIFY_OUT: relative(repo, out), PYTHONHASHSEED: '0', ...(full ? { GRAPHIFY_FORCE: '1' } : {}) }

function run(cmd, args, opts = {}) {
  const r = spawnSync(cmd, args, { cwd: repo, env, stdio: ['ignore', 'pipe', 'pipe'], encoding: 'utf8', ...opts })
  const text = (r.stdout + r.stderr).split('\n').filter((l) => !/skill is from graphify/.test(l)).join('\n').trim()
  if (r.status !== 0) { console.error(`${cmd} ${args.join(' ')} failed:\n${text}`); process.exit(r.status ?? 1) }
  return text
}

const t0 = Date.now()
console.log(run('graphify', ['extract', '.', '--code-only', ...(full ? ['--force'] : [])]).split('\n').slice(-3).join('\n'))
console.log(run('graphify', ['cluster-only', '.', '--graph', relative(repo, join(out, 'graph.json')), '--no-label', '--no-viz']).split('\n').slice(-1)[0])
if (existsSync(join(out, 'GRAPH_REPORT.md'))) copyFileSync(join(out, 'GRAPH_REPORT.md'), join(here, 'GRAPH_REPORT.md'))
console.log(run(process.execPath, [join(here, 'design-graph.mjs')]))
writeFileSync(join(here, 'god-nodes.json'), run('graphify', ['god-nodes', '--graph', relative(repo, join(here, 'graph.json')), '--top', '25', '--json']) + '\n')
console.log(run(process.execPath, [join(here, 'diagram.mjs')]))

console.log(`graph: done in ${((Date.now() - t0) / 1000).toFixed(1)}s (${full ? 'full' : 'update'})`)
