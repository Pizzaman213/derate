# audit

The audit log, the Settings → Audit log sub-tab (`SettingsTab.tsx`; it was
the `/audit` destination until 2026-11-18): every admin action, newest first, with what it
changed. It reads `GET /api/audit` (filters: time range, actor, action,
target) and nothing else; the log has no delete path on the server and none
here.

The server writes the whole object on each side of an edit
(`authz.key_dict`, `authz.org_dict`, a `LocalPrice`); the table shows only the
leaves that moved. None of those carries a key secret or hash, so nothing here
can render one.

Every row is one line: the Changes cell shows the first changed leaf and
`(+N more)`, with the full list in its tooltip. Double-clicking a row (or
Enter on a focused one) opens that row's actor as a second sub-tab inside the
card -- the event in full, the addresses the actor acted from, every key it
created (its `key_created` rows, so a key deleted since is still listed) and
its whole history, all time. That is what you re-check after a suspected
intrusion. One actor tab at a time, plain React state (not the URL); a row
with no actor does not open one, because an empty `actor` filter is no filter.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `AuditTab.tsx` | 221 | the Log / Actor tablist, the filters, the log's page |
| `AuditTable.tsx` | 183 | the one-line table, `TargetCell`, `Pager` -- shared by both views |
| `ActorPanel.tsx` | 285 | the actor view: this event, seen from IPs, keys created, all activity |
| `diff.ts` | 243 | before -> after reduced to the changed leaves (`limits.rpm`, not `limits`); null and absent stay different facts; one change as one line; the actor view's queries and IP summary. Pure |
| `audit.check.mjs` | 170 | hermetic: `diff.ts`, the query the filters send, the actor queries and `ipSummary` |
| `log.check.mjs` | 68 | `// requires: coordinator`: the live log is newest first and the action filter narrows the page |

The Admins and Local model pricing cards live in `../settings/` (`AdminsCard.tsx`, `PricingCard.tsx`).
