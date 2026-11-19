# requests

The request log, `/usage/requests` (the Requests pane of Usage): one row per client request, cluster-wide,
newest first. It reads `GET /api/requests`, which returns the *final* attempt
of each request with `attempts` set to the true count, so a cross-provider
retry is one row reading `2 attempts`, not two rows with one counted as an
error. Filters live in the URL (`?win= ?key= ?model= ?status= ?target=
?provider= ?retried=1`); `status=429` means rate-limited, not every failed
request. `/usage/requests/<request id>` is one request's detail.

## Layout

| File | What it owns |
|---|---|
| `RequestsSub.tsx` | the filter bar, request-id search, CSV link and paging; `/usage/requests/<id>` renders `RequestDetail` instead |
| `RequestDetail.tsx` | every recorded fact about one request, and its attempt chain |
| `RateLimitsSub.tsx` | Usage's Rate limits pane: model/provider statistics, live cooldowns and durable 429 log |
| `RequestRows.tsx` | the table, shared with a key's Recent requests (`../keys/KeyStats.tsx`) |
| `servedBy.ts` | "Served by" wording (provider, or deployment on nodes) and the window -> `start=` mapping. Pure |
| `requests.check.mjs` | hermetic: every `servedBy` branch and `windowStart` |

The chat transcript's request id links here (`../chat/Transcript.tsx`).

## Rate limits

`/usage/rate-limits` shows the raw archive's retained 429s, with 24h/7d/30d
windows, provider/model/target filters, pagination and CSV. Counters use the
archive's full matching totals, not just the visible page; the rate is 429s
divided by all matching requests. Current cooldowns are separate live facts.

`/usage?dep=claude-opus-5-5` opens this pane for that model and canonicalizes
to `/usage/rate-limits?dep=claude-opus-5-5`. A live deployment ID resolves to
its served name. Settings links here from each provider and model; exact
target history survives served-name changes. Switching to Requests carries
the selection and a 429 filter. Switching to Overview clears `dep` so the
model-specific default cannot pull the operator back to Rate limits.

New upstream HTTP and translated in-band 429s retain sanitized
`error_message` and `retry_after_s` in their attempt-chain JSON. Started
streams keep their HTTP 200 wire status, but their recorded logical failure
is 429. Requests labels those failures and detail shows the recorded refusal
and retry delay. Older rows without these fields say not recorded; a 503 is
never relabelled from a model's current cooldown. The full reason is also in
the existing provider/model cooldown log in `proxy.log`.

2026-11-18: added the Rate limits pane and provider-scoped request history;
extended `.omp/tests/ui/providers-status.check.mjs` to exercise statistics,
recovery, deep links, request navigation and accessible narrow layouts.

Two premade donuts summarize **failed requests only**. **Request errors**
follows the current model, target, provider and time window: explicit 429s
versus other failures, using the archive's `failed` status filter for the total.
**Errors by model** spans all models for the selected provider and time window,
deliberately independent of the model and target selectors. It sums every
usage bucket's `failed` count, not the visible log page; models without failures
are excluded. Successful requests never contribute to either chart.
Each donut shows its error total in the centre; there is no numeric counter
strip. The existing responsive chart grid stacks the pair on narrow screens.

The top row combines model/provider filters with right-aligned 24h/7d/30d
buttons, defaulting to 30 days. The repeated heading, explanatory paragraph
and reporting timestamp are removed.
The model donut hides the repeated legend; slice labels, tooltips and its
accessible table still identify each model. Both charts refresh every 15 seconds.
