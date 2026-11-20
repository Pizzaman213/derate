// The audit log's before -> after reduction, and the query it sends.
//
// Hermetic: no coordinator. What is worth checking is not a type -- that a
// nested limit change names `limits.rpm` and not `limits`, that null and absent
// stay different facts, that a login with no objects yields no changes rather
// than a crash, and that the order is stable.
//
//   cd ui && node src/tabs/audit/audit.check.mjs

import { load, report } from '../../check/harness.mjs'

const M = await load(import.meta.url, './diff.ts')
const { check, done } = report()

const paths = (cs) => cs.map((c) => c.path).join(',')

// No objects at all: a login, a logout, a cycle reset.
check(M.diffEntry(null, null).length === 0, 'both sides null -> no changes')
check(M.diffEntry(undefined, undefined).length === 0, 'both sides absent -> no changes')
check(M.diffEntry({ a: 1, b: [1, 2] }, { a: 1, b: [1, 2] }).length === 0, 'equal objects -> no changes')

// Nested dicts recurse to the leaf that moved.
{
  const cs = M.diffEntry(
    { name: 'k', limits: { rpm: 10, tpm: 100 }, notes: '' },
    { name: 'k', limits: { rpm: 20, tpm: 100 }, notes: '' },
  )
  check(cs.length === 1 && cs[0].path === 'limits.rpm', `nested change is one leaf (${paths(cs)})`)
  check(cs[0].kind === 'changed' && cs[0].before === 10 && cs[0].after === 20, 'nested change carries both values')
}
{
  const cs = M.diffEntry({ limits: { rpm: 10 } }, { limits: { rpm: 10, tpm: 5 } })
  check(cs.length === 1 && cs[0].path === 'limits.tpm' && cs[0].kind === 'added', 'a key added inside a dict is added')
  check(cs[0].before === undefined, 'an added field has an absent before')
}
{
  const cs = M.diffEntry({ limits: { rpm: 10, tpm: 5 } }, { limits: { rpm: 10 } })
  check(cs.length === 1 && cs[0].kind === 'removed' && cs[0].after === undefined, 'a removed field has an absent after')
}

// Null is a value, distinct from absent.
{
  const cs = M.diffEntry({ allowed_models: ['a'] }, { allowed_models: null })
  check(cs.length === 1 && cs[0].kind === 'changed' && cs[0].after === null, 'list -> null is a change to null')
  check(M.formatValue(cs[0].after) === 'null', 'null renders as null')
  check(M.formatValue(undefined) === '\u2014', 'absent renders as an em dash')
  check(M.formatValue(0) === '0', 'zero renders as 0, not a dash')
  check(M.formatValue('5') === '"5"' && M.formatValue(5) === '5', 'a string and a number are told apart')
  check(M.formatValue('null') !== M.formatValue(null), 'the string "null" is not null')
  check(M.formatValue(['a', 'b']) === '["a","b"]', 'an array renders as JSON')
}
{
  const cs = M.diffEntry({ cloud_fallback_local_model: null }, {})
  check(cs.length === 1 && cs[0].kind === 'removed' && cs[0].before === null, 'null present -> absent is a removal')
}

// Arrays are one value.
{
  const cs = M.diffEntry({ allowed_models: ['a'] }, { allowed_models: ['a', 'b'] })
  check(cs.length === 1 && cs[0].path === 'allowed_models', 'an array that grew is one change')
}

// A create and a delete list every leaf on the side that exists.
{
  const cs = M.diffEntry(null, { name: 'o', limits: { rpm: 5, tpm: 9 }, default_key_limits: {} })
  check(paths(cs) === 'default_key_limits,limits.rpm,limits.tpm,name', `create lists every leaf (${paths(cs)})`)
  check(cs.every((c) => c.kind === 'added'), 'create is all added')
  const empty = cs.find((c) => c.path === 'default_key_limits')
  check(empty && typeof empty.after === 'object' && Object.keys(empty.after).length === 0, 'an empty dict is still a leaf')
}
{
  const cs = M.diffEntry({ model: 'm', input_usd_per_mtok: 1 }, null)
  check(cs.length === 2 && cs.every((c) => c.kind === 'removed'), 'delete is all removed')
}

// Ordering is by path, whatever the server's key order.
{
  const cs = M.diffEntry({ z: 1, a: 1, m: { b: 1, a: 1 } }, { z: 2, a: 2, m: { b: 2, a: 2 } })
  check(paths(cs) === 'a,m.a,m.b,z', `sorted by path (${paths(cs)})`)
}

// Dict <-> scalar is a change of the whole field, not a recursion.
{
  const cs = M.diffEntry({ routing_policy: null }, { routing_policy: { kind: 'x' } })
  check(cs.length === 1 && cs[0].path === 'routing_policy' && cs[0].kind === 'changed', 'null -> dict is one change')
}

// The query.
{
  const now = 1_000_000_000
  const q = M.auditQuery({ actor: ' ', action: '', target: '', range: 'all', page: 1 }, now)
  check(!('actor' in q) && !('action' in q) && !('target' in q) && !('start' in q), 'empty filters are omitted')
  check(q.page === 1 && q.page_size === M.AUDIT_PAGE_SIZE, 'page and page size are sent')
  const q2 = M.auditQuery({ actor: 'root', action: 'key_created', target: 'abc', range: '24h', page: 3 }, now)
  check(q2.actor === 'root' && q2.action === 'key_created' && q2.target === 'abc', 'filters are sent')
  check(q2.start === now - 86400 && q2.page === 3, '24h starts a day ago')
  check(M.auditQuery({ actor: '', action: '', target: '', range: '7d', page: 0 }, now).page === 1, 'page floors at 1')
  check(M.auditQuery({ actor: '', action: '', target: '', range: '30d', page: 1 }, now).start === now - 30 * 86400, '30d')
}

check(M.AUDIT_ACTIONS.length === 50 && M.AUDIT_ACTIONS[0] === 'login' && M.AUDIT_ACTIONS[49] === 'node_runtime_model_changed', 'every action, contract order')
check(new Set(M.AUDIT_ACTIONS).size === M.AUDIT_ACTIONS.length, 'no action listed twice')
check(M.pageCount(0, 50) === 1 && M.pageCount(50, 50) === 1 && M.pageCount(51, 50) === 2, 'page count')

check(
  JSON.stringify(M.targetDest('key', 'k1')) === '{"dest":"usage","sub":"keys","key":"k1"}',
  'a key links to /usage/keys/<id>',
)
check(M.targetDest('org', 'o1') === null, 'an org has no screen to link to')
check(M.targetDest('admin', 'root') === null && M.targetDest('pricing', 'm') === null, 'other targets do not link')
check(M.targetDest('key', '') === null, 'an empty id does not link')

// One change as one line of text (the log row's first change, and each
// line of its tooltip).
check(
  M.changeText({ path: 'limits.rpm', kind: 'changed', before: 60, after: 120 }) === 'limits.rpm 60 \u2192 120',
  'a change reads path before → after',
)
check(
  M.changeText({ path: 'name', kind: 'added', before: undefined, after: 'x' }) === 'name \u2014 \u2192 "x"',
  'an added field reads as dash → value',
)
check(
  M.changeText({ path: 'allowed_models', kind: 'changed', before: ['a'], after: null }) === 'allowed_models ["a"] \u2192 null',
  'null stays null, not a dash',
)

// Actor drill-down queries: all time, actor verbatim.
{
  const q = M.actorEventsQuery('root', 0)
  check(
    JSON.stringify(q) === '{"actor":"root","page":1,"page_size":50}' && !('start' in q) && !('end' in q) && !('action' in q),
    'actor events: page clamped to 1, no window, no action',
  )
  const k = M.actorKeysQuery(' a b ')
  check(k.actor === ' a b ', 'actor keys: actor sent verbatim, not trimmed (server matches exactly)')
  check(k.action === 'key_created' && k.page === 1 && k.page_size === 1000 && !('start' in k), 'actor keys: key_created, all time, server cap')
  check(JSON.stringify(M.actorIpsQuery('root')) === '{"actor":"root","page":1,"page_size":1000}', 'actor ips: newest 1000, all time')
}

// Addresses an actor acted from.
{
  const entries = [
    { ip: '1.1.1.1', ts: 10 },
    { ip: '2.2.2.2', ts: 30 },
    { ip: '1.1.1.1', ts: 20 },
    { ip: null, ts: 5 },
  ]
  const before = JSON.stringify(entries)
  const s = M.ipSummary(entries)
  check(
    JSON.stringify(s) ===
      JSON.stringify([
        { ip: '2.2.2.2', count: 1, first: 30, last: 30 },
        { ip: '1.1.1.1', count: 2, first: 10, last: 20 },
        { ip: null, count: 1, first: 5, last: 5 },
      ]),
    'ip summary: grouped, first/last, newest last-seen first, null its own group',
  )
  check(JSON.stringify(entries) === before, 'ip summary does not mutate its input')
  check(M.ipSummary([]).length === 0, 'ip summary of nothing is nothing')
  const tie = M.ipSummary([
    { ip: null, ts: 7 },
    { ip: 'b', ts: 7 },
    { ip: 'a', ts: 7 },
  ])
  check(tie.map((x) => x.ip).join(',') === 'a,b,', 'equal last-seen: by address, unrecorded last')
}

done()
