# keys

The Keys pane of Usage: `/usage/keys` lists every `derate-` API key, `/usage/keys/<key id>` is one
key's page. Everything comes from `GET /api/keys` and `GET /api/keys/{key_id}`
(limit states, 30-day requests/tokens/cost), `GET /api/usage` and
`GET /api/keys/{key_id}/requests`; nothing here computes a figure the server
did not send.

A secret is drawn in exactly one place, `components/RevealModal`, after
`POST /api/keys` or `POST /api/keys/{key_id}/rotate` -- the two key responses
`client.ts` fetches unscrubbed. It is dropped on "I saved it". Both ask for the
signed-in admin's password (`current_password`) on every call, so a stolen
session alone cannot mint a credential: the create form, every rotate and the
Models screen's "Create a key for this model" each carry a password field.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `KeysSub.tsx` | 204 | the table, its filters (status, near limit, expiring within 1/7/30 days) and search, with "New key" in the card heading; routes `/usage/keys/<id>` to `KeyDetail` |
| `KeyCreate.tsx` | 332 | the "New key" button and the create form it opens as a modal (Escape/scrim/Cancel drop the draft): name, expiry from `KEY_EXPIRY_DAYS`, limits, models and routing; then the one-time reveal |
| `KeyDetail.tsx` | 197 | one key: what it is, what can be done to it, thirty days of what it did. A deleted or expired key still reads |
| `KeyActions.tsx` | 188 | disable / enable / rotate / delete / raise / reset-cycle, each confirmed; admin keys rotate but do not delete |
| `KeyStats.tsx` | 252 | the charts (`dashboard/Chart.tsx`), why a measure is absent, three breakdowns, and the raw requests with a CSV link |
| `LimitsEditor.tsx` | 163 | limit rows: a fixed type or `model:<served name>:<suffix>` |
| `rows.ts` | 396 | the arithmetic and wording: tightest limit, near/at, lamp per status and per limit, null-vs-0, per-field error chips. Pure |
| `keys.check.mjs` | 123 | hermetic: `rows.ts` thresholds at their edges, the lamp for each status and each limit, null-vs-0, the error chip text |
| `table.check.mjs` | 51 | `// requires: coordinator`: the rows built from `GET /api/keys` equal the server's one for one -- ids, order, statuses, tightest limit, near/at |
