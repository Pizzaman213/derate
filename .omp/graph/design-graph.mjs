#!/usr/bin/env node
// Design nodes and edges, in graphify's own node-link shape, merged into the
// code graph.
//
//   node graph/design-graph.mjs      (build.mjs runs it after extract/update)
//
// Reads graphify-out/graph.json, adds token / icon / component / spec nodes
// and the edges that join them to the code, writes design-graph.json (the
// design part alone) and graph.json (code + design). `graphify merge-graphs`
// is NOT used: it is a cross-repo tool that prefixes every node id with a repo
// tag and flattens to an undirected simple graph, which breaks id-based links
// and the direction `affected` depends on -- so the merge is a plain append in
// the same schema, ids preserved.
//
// Node ids follow graphify's style (snake, no punctuation) with a `design_`
// prefix; labels keep the human form (`--ink`, `icon:close`,
// `component:Readout`, `DESIGN.md#readout`). Every edge is EXTRACTED: each one
// is a fact grep found in a file, never an inference.

import { readFileSync, writeFileSync, readdirSync, statSync, existsSync } from 'node:fs'
import { dirname, join, relative, resolve, basename } from 'node:path'
import { fileURLToPath } from 'node:url'
import { build as buildTokens, loadTokens } from '../design/tokens/build.mjs'
import { loadMeta as loadIcons } from '../design/icons/build.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const repo = resolve(here, '..', '..')
const codePath = join(here, 'graphify-out', 'graph.json')
const outDesign = join(here, 'design-graph.json')
const outMerged = join(here, 'graph.json')

const code = JSON.parse(readFileSync(codePath, 'utf8'))
const byId = new Map(code.nodes.map((n) => [n.id, n]))
const fileNode = new Map()
for (const n of code.nodes) if (n.source_file && n.source_location === 'L1' && n.label === basename(n.source_file)) fileNode.set(n.source_file, n)
// fall back: the first node per source_file that has no parent
for (const n of code.nodes) if (n.source_file && !fileNode.has(n.source_file)) fileNode.set(n.source_file, n)

const slug = (s) => s.replace(/[^A-Za-z0-9]+/g, '_').replace(/^_+|_+$/g, '').toLowerCase()
const nodes = [], links = []
const seen = new Set()
const addNode = (n) => { if (!seen.has(n.id)) { seen.add(n.id); nodes.push({ file_type: 'design', _origin: 'design-pack', community: null, norm_label: n.label.toLowerCase(), ...n }) } }
const addEdge = (source, target, relation, source_file, extra = {}) => links.push({ relation, confidence: 'EXTRACTED', confidence_score: 1, weight: 1, source_file, source_location: 'L1', _origin: 'design-pack', source, target, ...extra })

// ------------------------------------------------------------ tokens
const tokens = buildTokens(loadTokens())
const tokenId = (css) => `design_token_${slug(css)}`
const primId = (key) => `design_primitive_${slug(key)}`
for (const n of tokens.named) {
  addNode({ id: tokenId(n.css.slice(2)), label: n.css, type: 'token', layer: n.layer, token_type: n.leaf.$type, themed: n.themed ?? false, description: n.leaf.$description, source_file: '.omp/design/tokens/tokens.json', source_location: n.key })
}
const cssByKey = new Map(tokens.named.map((n) => [n.key, n.css.slice(2)]))
for (const n of tokens.named) {
  for (const ref of n.leaf.$value && typeof n.leaf.$value === 'object' ? Object.values(n.leaf.$value) : [n.leaf.$value]) {
    for (const m of String(ref).matchAll(/\{([a-zA-Z0-9_.-]+)\}/g)) {
      const key = m[1]
      if (cssByKey.has(key)) {
        addEdge(tokenId(n.css.slice(2)), tokenId(cssByKey.get(key)), n.layer === 'component' ? 'component_of' : 'semantic_of', '.omp/design/tokens/tokens.json')
      } else {
        addNode({ id: primId(key), label: key, type: 'primitive', layer: 'primitive', source_file: '.omp/design/tokens/tokens.json', source_location: key })
        addEdge(tokenId(n.css.slice(2)), primId(key), 'semantic_of', '.omp/design/tokens/tokens.json')
      }
    }
  }
}

// ------------------------------------------------------------ icons
const icons = loadIcons()
const iconId = (name) => `design_icon_${slug(name)}`
for (const i of icons.icons) addNode({ id: iconId(i.name), label: `icon:${i.name}`, type: 'icon', category: i.category, tags: i.tags, replaces: i.replaces, source_file: `.omp/design/icons/svg/${i.name}.svg` })
const iconNames = new Set(icons.icons.map((i) => i.name))

// ------------------------------------------------------------ components
const registry = JSON.parse(readFileSync(join(here, '..', 'design', 'components', 'registry.json'), 'utf8'))
const compId = (name) => `design_component_${slug(name)}`
const specId = (anchor) => `design_spec_${slug(anchor)}`
const compFiles = new Map()
for (const c of registry.components) {
  addNode({ id: compId(c.name), label: `component:${c.name}`, type: 'component', kind: c.kind, source_file: c.path, states: c.states, tests: c.tests, spec: c.spec })
  compFiles.set(c.path, [...(compFiles.get(c.path) ?? []), c.name])
  // the code node: a symbol in that file with the same label, else the file node
  const sym = code.nodes.find((n) => n.source_file === c.path && n.label === c.name) ?? fileNode.get(c.path)
  if (sym) addEdge(compId(c.name), sym.id, 'defined_in', c.path)
  if (c.spec) {
    const anchor = c.spec.split('#')[1]
    addNode({ id: specId(anchor), label: `DESIGN.md#${anchor}`, type: 'spec', source_file: '.omp/design/DESIGN.md', source_location: `#${anchor}` })
    addEdge(compId(c.name), specId(anchor), 'specified_in', '.omp/design/DESIGN.md')
  }
  if (c.tests) {
    const t = fileNode.get(c.tests) ?? (() => { const id = `design_test_${slug(c.tests)}`; addNode({ id, label: basename(c.tests), type: 'test', source_file: c.tests }); return { id } })()
    addEdge(compId(c.name), t.id, 'tested_by', c.tests)
    // `graphify affected` walks a fixed relation list in reverse (calls,
    // references, imports…); the check file does reference the component, and
    // this edge is what lets `affected "component:X"` find it.
    addEdge(t.id, compId(c.name), 'references', c.tests)
  }
  for (const icon of c.icons ?? []) if (iconNames.has(icon)) addEdge(compId(c.name), iconId(icon), 'uses_icon', c.path)
}

// ------------------------------------------------------------ usage edges from source
function walk(dir, out = []) {
  for (const n of readdirSync(dir)) {
    const p = join(dir, n)
    if (statSync(p).isDirectory()) { if (n !== 'node_modules' && n !== 'dist') walk(p, out) }
    else if (/\.(tsx?|css|mjs)$/.test(n)) out.push(p)
  }
  return out
}
const files = [...walk(join(repo, 'ui', 'src')), ...walk(join(repo, '.omp', 'tests'))]
const tokenNames = new Set(tokens.named.map((n) => n.css.slice(2)))
for (const p of files) {
  const rel = relative(repo, p)
  const text = readFileSync(p, 'utf8')
  const owner = fileNode.get(rel)
  const owners = [owner?.id, ...(compFiles.get(rel) ?? []).map(compId)].filter(Boolean)
  if (!owners.length) continue
  const used = new Set()
  for (const m of text.matchAll(/var\(--([a-z0-9-]+)[,)]/g)) if (tokenNames.has(m[1])) used.add(m[1])
  for (const m of text.matchAll(/tokens\.(?:light|dark|static|\[['"]high-contrast['"]\])(?:\.([a-zA-Z0-9]+)|\[['"]([a-z0-9-]+)['"]\])/g)) { const n = m[1] ?? m[2]; if (tokenNames.has(n)) used.add(n) }
  for (const m of text.matchAll(/--(tag|avatar)-\$\{/g)) for (let i = 1; i <= 10; i++) if (tokenNames.has(`${m[1]}-${i}`)) used.add(`${m[1]}-${i}`)
  for (const t of used) for (const o of owners) addEdge(o, tokenId(t), 'uses_token', rel)
  if (/components\/Icon'|components\/Icon\.tsx'|\.\/Icon'/.test(text) || rel.endsWith('components/Icon.tsx')) {
    const icons = new Set()
    for (const m of text.matchAll(/name=(?:"([a-z0-9-]+)"|\{([^}]*)\})/g)) {
      if (m[1]) { if (iconNames.has(m[1])) icons.add(m[1]) }
      else for (const q of m[2].matchAll(/'([a-z0-9-]+)'/g)) if (iconNames.has(q[1])) icons.add(q[1])
    }
    for (const i of icons) for (const o of owners) addEdge(o, iconId(i), 'uses_icon', rel)
  }
}
// renders: a registry component whose file imports another registry component's file.
// references: any file that imports a component's file references the component
// (the reverse edge `affected` traverses).
const compFileIds = new Set([...compFiles.keys()].map((f) => fileNode.get(f)?.id).filter(Boolean))
for (const e of code.links) {
  if ((e.relation === 'imports' || e.relation === 'imports_from') && compFileIds.has(e.target) && e.source !== e.target) {
    const file = [...compFiles].find(([f]) => fileNode.get(f)?.id === e.target)
    if (file) for (const name of file[1]) addEdge(e.source, compId(name), 'references', e.source_file ?? '')
  }
}
const compByFile = new Map([...compFiles].map(([f, names]) => [fileNode.get(f)?.id, names]))
for (const e of code.links) {
  if (e.relation !== 'imports' && e.relation !== 'imports_from') continue
  const from = compByFile.get(e.source), to = compByFile.get(e.target)
  if (from && to && e.source !== e.target) for (const a of from) for (const b of to) addEdge(compId(a), compId(b), 'renders', e.source_file ?? '')
}

// dedupe edges
const key = (e) => `${e.source}|${e.target}|${e.relation}`
const uniq = [...new Map(links.map((e) => [key(e), e])).values()].filter((e) => byId.has(e.source) || seen.has(e.source)).filter((e) => byId.has(e.target) || seen.has(e.target))

const design = { directed: code.directed, multigraph: code.multigraph, graph: { ...code.graph, design_pack: true }, nodes, links: uniq, hyperedges: [] }
writeFileSync(outDesign, JSON.stringify(design, null, 1) + '\n')
const merged = { ...code, nodes: [...code.nodes, ...nodes], links: [...code.links, ...uniq] }
writeFileSync(outMerged, JSON.stringify(merged, null, 1) + '\n')
const rel = {}
for (const e of uniq) rel[e.relation] = (rel[e.relation] ?? 0) + 1
console.log(`design graph: ${nodes.length} nodes, ${uniq.length} edges (${Object.entries(rel).map(([k, v]) => `${k} ${v}`).join(', ')}); merged ${merged.nodes.length} nodes, ${merged.links.length} edges -> .omp/graph/graph.json`)
