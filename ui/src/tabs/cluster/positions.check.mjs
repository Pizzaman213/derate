// positions.ts: a stored arrangement reads back exactly, or not at all.
//
//   cd ui && node src/tabs/cluster/positions.check.mjs
import { load, report } from '../../check/harness.mjs'

const P = await load(import.meta.url, './positions.ts')
const { check, done } = report()

const good = JSON.stringify({ v: 1, positions: { input: { x: 1, y: 2 }, 'split:a': { x: -3.5, y: 0 } } })
check(JSON.stringify(P.parsePositions(good)) === JSON.stringify({ input: { x: 1, y: 2 }, 'split:a': { x: -3.5, y: 0 } }), 'a good record reads back exactly')
check(Object.keys(P.parsePositions(null)).length === 0, 'nothing stored is nothing')
check(Object.keys(P.parsePositions('{not json')).length === 0, 'garbage is dropped')
check(Object.keys(P.parsePositions('[1,2]')).length === 0, 'an array is dropped')
check(Object.keys(P.parsePositions(JSON.stringify({ v: 2, positions: { a: { x: 1, y: 1 } } }))).length === 0, 'another version is dropped')
check(Object.keys(P.parsePositions(JSON.stringify({ positions: { a: { x: 1, y: 1 } } }))).length === 0, 'no version is dropped')

const many = Object.fromEntries(Array.from({ length: P.MAX_ENTRIES + 1 }, (_, k) => [`b${k}`, { x: k, y: k }]))
check(Object.keys(P.parsePositions(JSON.stringify({ v: 1, positions: many }))).length === 0, `more than ${P.MAX_ENTRIES} entries is dropped whole`)
const exactly = Object.fromEntries(Array.from({ length: P.MAX_ENTRIES }, (_, k) => [`b${k}`, { x: k, y: k }]))
check(Object.keys(P.parsePositions(JSON.stringify({ v: 1, positions: exactly }))).length === P.MAX_ENTRIES, `exactly ${P.MAX_ENTRIES} is kept`)

// JSON cannot carry NaN; a hand-edited record can carry strings and nulls.
const mixed = P.parsePositions('{"v":1,"positions":{"ok":{"x":1,"y":1},"str":{"x":"1","y":1},"nul":{"x":null,"y":1},"none":null,"big":{"x":1e400,"y":0}}}')
check(JSON.stringify(Object.keys(mixed)) === '["ok"]', 'a non-finite or non-numeric coordinate drops that entry only')

done()
