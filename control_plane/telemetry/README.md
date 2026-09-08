# telemetry

The numbers this product is built on exist for one instant and are then thrown
away: `proxy.settle()` computes tokens, TTFT, decode and duration, folds them
into an EWMA, and the raw figures are gone. This package writes them down, on
the machine that produced them, before anything is averaged.

Three layers, and the boundary between them is a cursor rather than a message.
Every node appends to its own SQLite journal (`journal.py`). The coordinator
drains each journal over the node agent's `GET /agent/journal` (`collector.py`)
into a typed archive (`archive.py`). A pass on a timer rolls raw rows into
1-minute and 1-hour buckets and enforces every retention horizon
(`retention.py`), and `query.py` reads it back at whatever resolution the
window can afford.

One rule governs the whole package: **a telemetry failure stays a telemetry
failure.** Nothing here may block a caller, fail a startup, or raise into a
producer's error handling. It is off entirely when its data root does not
exist, so a development machine and `pytest` record nothing without anyone
opting out.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `journal.py` | 627 | L1: the per-node append-only journal, its writer thread, and drop-oldest overflow |
| `collector.py` | 220 | L2: draining journals into the archive, pull not push, idempotent, no ack |
| `archive.py` | 539 | L3: the coordinator's typed archive, and transactional ingest |
| `retention.py` | 371 | the 1-minute and 1-hour rollups, and every retention horizon, on a timer |
| `query.py` | 372 | reading it back: resolution chosen for you, gaps reported |
| `service.py` | 256 | the `Telemetry` bundle, the one switch that turns it all off, and the lifecycle |
| `config.py` | 237 | paths, horizons, env-var bindings, and the byte arithmetic behind each default |
| `records.py` | 223 | the four record kinds and the `TelemetrySink` Protocol every producer writes through |
| `loghandler.py` | 156 | structured logs into the journal, as a handler on the root logger |
| `events.py` | 155 | the deploy and gateway event buses tapped into the journal, with a `source` |
| `hist.py` | 141 | the log-spaced latency histogram: fixed cost, exact merge |
| `__init__.py` | 37 | the export surface: the sink, the record types, and the `KIND_*` constants |

## `journal.py`

One SQLite file in WAL mode, one background writer thread, and a
`deque(maxlen=JOURNAL_QUEUE_MAX)` in front of it. `Journal.append()` never
blocks and never raises; past 8192 buffered rows the deque drops the oldest and
`_dropped` moves, the same policy `deploy/events.py` applies to a subscriber
that stops draining, for the same reason — losing the tail of a burst is
recoverable, stalling the producer is not. `start()`, `close()`, `sync()`,
`trim()`, `read()`, `stats()` and `open_journal()` are the surface; the four
`TelemetrySink` methods write into one table, because collection then needs one
cursor instead of four.

**The pragma order in `_connect` is the whole retention story.** SQLite accepts
an `auto_vacuum` change only while the page size is unfixed, and
`PRAGMA journal_mode=WAL` fixes it — so setting it afterwards is a no-op that
reports no error. With `auto_vacuum=NONE` every `PRAGMA incremental_vacuum` is
also a silent no-op, freed pages stay in the file, and `_file_bytes` can never
fall back under the cap once it has crossed, so `_enforce_size` evicts the whole
journal on every trim, forever. Measured before the line moved: a 540 MB
journal, 540 MB of it freelist, holding one row. `ensure_incremental_vacuum()`
converts a file written before the fix, and is called from `trim()` on the
writer thread rather than at open — `registry/startup.py` builds the journal
before `node_agent.start()`, so a stall there is a node that never begins
listening with no log line to say why.

**A trimmed window says it was trimmed.** Past `JOURNAL_MAX_BYTES` (512 MiB)
the oldest rows go even if they were never collected, and `_note_gap` writes a
`telemetry_gap` event through the ordinary path so it lands in the archive's
`gaps` table. Two rules there are load-bearing: the marker is written *after*
the eviction loop, never inside it — noting each batch kept refilling the table
the loop was waiting to empty, which produced 371,530 identical log lines in one
minute on a real coordinator — and only rows past `shipped_hwm` count toward the
hole, on `min`/`max` of their timestamps rather than first/last, because `ts` is
caller-supplied and not strictly monotonic in `seq`. A zero-width gap is not a
fact and is never written; 772 of them are still sitting in the live archive
from the old loop.

**`read(since=N)` is itself the acknowledgement.** Asking for rows after N says
everything through N is safe on the coordinator, so it advances `shipped_hwm`
and the protocol needs one endpoint and no ack message. `_LIVE` tracks journals
by absolute path and logs a warning when a second one starts on the same file in
one process — invisible from outside, so it is said out loud rather than left to
be inferred from a doubled CPU cost.

## `collector.py`

Pull, not push. The coordinator already pulls health and telemetry from every
member over the same `AgentClient` seam, so `HttpSource` reuses a transport that
exists, is bounded by `SHIP_TIMEOUT_S`, and is testable without a network. It
also puts backpressure where the disk is: a coordinator that cannot keep up asks
for less. `LocalSource` drains the coordinator's own journal through the
identical ingest path, so the single-node case is trustworthy before a second
machine exists.

**Every SQLite call goes through `asyncio.to_thread`.** The gateway's event loop
must never wait on a disk — `tests/load/report.py` fails a run whose loop-lag p99
passes 250 ms, and that is exactly what a careless commit here produces. Two
constants exist for the same reason: `MAX_PAGES` (32) bounds one node's share of
a round, and `SHIP_PAGE_PAUSE_S` (0.005) paces the pages. Yielding alone is not
enough; `asyncio.sleep(0)` gives the loop one turn and the next page's
parse-and-insert begins before the requests behind the last one have drained.
Measured at 400 rps, the unpaced collector put ~15 ms on the gateway's p99.

`poll_once()` degrades one node at a time: a failure is logged and recorded via
`archive.note_ship_failure` rather than ending the round. `rewire()` exists
because a composed node starts telemetry twice — `start_node()` before there is
a registry, the gateway's lifespan after — and replacing the collector would
leak the first one's task while refusing the second call would leave the
coordinator unable to reach its workers.

## `archive.py`

Journal rows arrive as opaque JSON and become typed rows here: `samples`,
`requests`, `events`, `logs`, plus `cursors`, `gaps` and the two `rollup_*`
tables. `Archive.ingest()` runs one `BEGIN IMMEDIATE` per batch and advances
the cursor **inside the same transaction as the rows it covers**. That is what
makes collection idempotent — a coordinator that dies mid-batch re-asks from the
last committed cursor, gets the same rows, and the primary keys absorb the
repeat.

`requests` is keyed `(request_id, attempt_no)`: one row per attempt, not per
client request, so a failover produces two and the pair is the record of the
failover having happened. `_ingest_rows` groups a page and issues one
`executemany` per kind rather than a statement per row, because the collector
runs this in a worker thread while the gateway is serving and one uninterrupted
stretch of it shows up directly in the p99.

**`close()` takes the lock, and the bug it fixes is a segfault rather than an
exception.** Every other path took `_lock` before touching `_conn`; this one did
not, so a shutdown while a poll was mid-`INSERT` freed the sqlite3 connection
under the C extension still executing on it. Closing a handle a blocked writer
already captured is safe — sqlite3 raises `ProgrammingError` — closing it
*during* a statement is not.

`_note_dirty`/`take_dirty_from` carry the oldest timestamp in each batch to the
next compaction. A worker unreachable for an hour delivers an hour of backlog at
once, landing in buckets that compaction has already rolled; a clock-derived
watermark would silently under-count them.

## `retention.py`

`compact(archive)` rolls closed buckets and then enforces every horizon, and it
runs on a timer rather than on reconcile. That is a direct correction:
`deploy/store.py`'s `purge_expired()` is called only from `reconcile()`, which
runs once per restart, so a process that stays up never collects anything.

Rollups are what make a year affordable. Raw samples cost about 8 MB per node
per day; the 1-minute rows that summarise them cost about 10 KB, and the hourly
rows about 170 bytes. Only closed buckets are rolled — `ROLL_LAG_S` (120s) keeps
an in-flight second from producing a row that is wrong the instant after it is
written — and a pass re-rolls at least `REROLL_WINDOW_S` (900s) back, further if
`take_dirty_from` says a late batch arrived. Sample averages roll hour-ward
weighted by the sample count behind them, so an hour of one busy minute and
fifty-nine idle ones reports the truth.

The whole pass holds `archive.lock`. Compaction rewrites the tables the
collector is inserting into, and interleaving them would deadlock two
`BEGIN IMMEDIATE` transactions against each other rather than merely slowing
each down.

`_expire` scales five horizons together off `DERATE_TELEMETRY_RETENTION_DAYS`,
so halving it preserves the ratios between them: `samples` and `events` at 30
days, `requests` and `logs` at 7, and `gaps` on its `to_ts` against the `events`
window, whose holes it describes. The rollup horizons are deliberately not
scaled, because somebody shortening the raw window to save disk wants a shorter
raw window, not a shorter memory. `gaps` used to have no horizon at all — the
one table that grew forever, and not a detail, since `_envelope()` returns every
gap overlapping a window on *every* history query. The same pass deletes
zero-width rows (`DELETE FROM gaps WHERE to_ts <= from_ts`), which is what
clears the 772 the old eviction loop left behind. `_enforce_size` drops the
oldest raw day at `ARCHIVE_MAX_BYTES` (16 GiB) and keeps its rollups: losing
per-second detail to a disk ceiling is a trade, losing the fact that the day
happened is not.

## `query.py`

Two rules shape every function. **Pick the resolution, do not let the caller ask
for 2.6 million rows.** `pick_step()` defaults to `auto`: raw rows below
`AUTO_STEP_RAW_MAX_S` (6h), 1-minute rollups below `AUTO_STEP_1M_MAX_S` (7d),
hourly beyond, so dragging a date picker to "last year" costs the same as "last
hour". It is also never finer than the row budget can carry, and the budget is
shared across series — node samples land at 1 Hz *per node*, so a six-hour
window is ~21,600 rows each against a 5,000-row cap, and choosing RAW there does
not return six hours at full resolution, it returns the first 40 minutes of it.
`nodes()` therefore passes `series = 1 if node_id else max(1,
_node_count(archive))` — one named node divides nothing — and `_node_count`
counts off `cursors`, one row per node, rather than off the samples table.

**Say when data is missing and why.** `_envelope` puts `resolution`, `durable`
and every overlapping gap on every answer, because a flat line has two very
different causes — the cluster was idle, or the rows were trimmed. `durable` is
what separates an archive-backed answer from the registry's in-RAM ring, which
`/api/history/nodes` falls back to when telemetry is off and which labels itself
`resolution: "ring"`, `durable: false`.

`nodes()`, `requests()`, `events()` and `logs()` all select `ORDER BY ts DESC`,
so the `LIMIT` drops the far end of the window rather than the recent end.
`nodes()` and `requests()` then call `rows.reverse()` and answer in time order,
which is what a chart plots; `events()` and `logs()` answer newest-first as
read. A `level` filter means "this and worse" (`_at_least`), a `logger` filter
is a prefix, and `exclude` is the one negation on this surface —
it exists because the handler now declines to record the noisy loggers, and a
window recorded before that change would otherwise still read as a wall of them.

## `service.py`

`Telemetry` owns the journal, the archive, the collector and the gateway's event
bus, and is inert when disabled. Two rules govern construction. **Never fail to
start because telemetry cannot**: a data root that is missing, read-only or full
degrades to `NULL_SINK` and a log line, because the gateway serving tokens
matters more than the gateway remembering that it did. **Off unless there is
somewhere to write**: `from_env()` returns `disabled()` when the root is not a
directory, which is how a development machine and the test suite stay clean
without anyone opting out.

`start()` is called twice on a composed node — by `start_node()` before there is
a registry, and by the gateway's lifespan with the registry and providers
attached — and the second call must neither build a second collector nor simply
return. It rewires the running one. `gateway_events` is built on first use so a
worker never imports the deploy package that `EventBus` lives in;
`_agent_urls()` and `_agent_client()` are duck-typed, so a stub registry yields
no peers and the coordinator collects only itself. `capture_logs()` passes the
*provider service's own* redactor rather than a new one: a `Redactor` scrubs only
values it has been told to remember, and the provider service is what remembers
them.

## `config.py`

Local to this package, and nothing here is a frozen contract. Every horizon is a
default chosen against arithmetic rather than a round number: a node sample is
roughly 96 bytes on disk, so thirty days at 1 Hz is about 250 MB per node — but
a request row is roughly 260 bytes, which is 2.2 GB/day at a sustained 100 rps.
That asymmetry is why `REQUESTS_RAW_RETENTION_S` is 7 days against
`SAMPLES_RAW_RETENTION_S`'s 30. Six variables bind at runtime:
`DERATE_TELEMETRY`, `DERATE_TELEMETRY_RETENTION_DAYS`,
`DERATE_TELEMETRY_LOG_LEVEL`, `DERATE_TELEMETRY_MAX_BYTES`,
`DERATE_TELEMETRY_SHIP_INTERVAL_S` and `DERATE_TELEMETRY_QUIET_LOGGERS`.

`NOISY_LOGGERS` is `("uvicorn.access", "httpx")` and carries the measurement
that put it there: on a three-node cluster those two were **99.6%** of the log
stream and 81% of every row in the archive, and the journal was reaching its
byte cap in ~30 hours against a 72-hour retention — so access logging was eating
the coordinator-outage window the journal exists to provide. Nothing worth
keeping is lost, because every `/v1/*` request is already a row in `requests`
with tokens, TTFT, decode, duration, cost and retry reason. `uvicorn.error` is
deliberately not on the list: startup, shutdown and crash lines live there.
`SHIP_MAX_ROWS` is 500 rather than something larger for the same p99 reason as
`SHIP_PAGE_PAUSE_S` — at 2000 rows one page's ingest ran ~15 ms, at 500 it is
~4 ms.

## `records.py`

The seam every producer writes through. Four `KIND_*` constants share one
journal table; typing happens on the coordinator. `RequestRecord` is the row the
gateway had nowhere to put — target, provider, deployment, policy,
`strength_source`, `attempt_no`, `retry_reason`, status, both token counts,
`cost_usd`, TTFT, decode, duration, `parked_ms`, `admission_code`, `kv_bytes` —
and `tokens_estimated` says whether the completion count came from the
upstream's own `usage` block or from counting SSE frames.

`RequestTrace` is the mutable accumulator, created once in `openai_api._serve`
— the body `_proxy` and `_proxy_multipart` share, so a multipart transcription
is recorded exactly the way a chat completion is — and threaded through the
dispatch loop, because the facts a record needs are
spread across it: the model from the body, the policy and strength from the
`Selection`, prompt tokens and `kv_bytes` from the `AdmissionDecision`, and the
timings only from inside `proxy.settle()`. One object rather than six
parameters. `TelemetrySink` is a Protocol whose four methods must return
promptly and never raise, and `NULL_SINK` is the default, so `NodeAgent`,
`UpstreamProxy` and the deployment manager all still construct and unit-test on
a machine with telemetry switched off.

## `loghandler.py`

**A handler on the root logger, never a filter on a logger.** A
`logging.Filter` attached to a parent logger is never consulted for a child
logger's records, so a redaction filter installed on `control_plane.providers`
protected nothing until it was walked onto every child by hand — it shipped
inert once already. Every record reaches a root handler through the propagation
chain, and this is also the only thing that covers the gateway's loggers at all:
they are named `gateway.proxy`, `gateway.router` and so on, outside the
`control_plane.` hierarchy entirely.

`EXCLUDED_LOGGERS` — `control_plane.telemetry` and `asyncio` — is never
journalled at any setting, because the journal's own writer logs through this
handler and recording those records is a loop that ends when the disk fills.
That is correctness; `config.NOISY_LOGGERS` is volume, and the two are kept
apart so neither gets relaxed for the other's reason. The quiet check runs
*before* `record.getMessage()`, which does the %-formatting and at 17 dropped
records a second was the most expensive thing in `emit()`. A redactor that
raises means the line cannot be proved clean, so the line is not written.
`install()` must run *after* `uvicorn.run()` or with `log_config=None`:
uvicorn's config lands after `basicConfig` and sets `propagate=False` on its
access logger.

## `events.py`

`deploy/events.py` already produces the right shape — `{type, ts, **fields}` —
so `journal_events(bus, sink, source)` adds a tap and changes nothing about how
events are delivered. The `source` column is why the tap takes one:
"memory critical" from the deployment manager and "circuit open" from the
gateway are both real events and telling them apart later matters.
`SOURCE_DEPLOY` and `SOURCE_GATEWAY` are the only two `journal_events` is ever
called with; `SOURCE_REGISTRY` and `SOURCE_LINKS` are declared and have no tap.
Neither does the node agent use them: `registry/agent.py::note_shell_opened`
calls `sink.event("shell", ...)` with a literal, so a `source` in the archive is
not guaranteed to be one of these four names.

`GatewayEvents` is the gateway's own `EventBus` instance rather than the
deployment manager's — same class, no cross-package coupling — and its eleven
emitters each correspond to something the gateway wrote to a log line and
forgot: `breaker_opened`, `breaker_closed`, `retry_refused`, `request_parked`,
`park_resolved`, `admission_blocked`, `admission_cleared`,
`routing_source_failed`, `startup_degraded`, `restart_attempted`,
`restart_exhausted`. `_new_bus()` imports `deploy` lazily, because importing it
at module scope would put the whole deployment manager in every worker process.

## `hist.py`

A log-spaced latency histogram, moved here from `tests/load/driver.py`, which
still imports it. Two properties earn it a place in the archive. **Fixed cost**:
the bucket array is 920 ints however many samples went into it, so a busy minute
costs the same as a quiet one. **Exact merge**: rolling an hour from sixty
1-minute rows is a bucket-wise add, so an hourly p99 is a real p99 of the hour's
samples — not an average of sixty percentiles, which is the usual and quietly
wrong way to do this. That distinction is the entire reason `rollup_requests`
has blob columns.

2% relative bucket width (`_HIST_BASE = 1.02`), from 0.01 ms to about ten
minutes. `to_blob()` deflates an int array because most buckets are zero in any
real minute: a few dozen bytes rather than the ~7 KB the JSON form costs, and
there is one per minute per series.

## `__init__.py`

The import path producers use: `TelemetrySink`, `NULL_SINK`, `NullSink`,
`RequestRecord`, `RequestTrace`, the four `KIND_*` constants and `KINDS`. It
re-exports from `records.py` and nothing else, which is what lets
`registry/agent.py` import it at module scope on every worker without pulling
in SQLite, the archive or the deploy package.

## The seam with the gateway and the registry

Five gateway modules import this package directly:

- **`gateway/deps.py`** defaults `GatewayDeps.sink` to `NULL_SINK` in
  `__post_init__`, so no call site has to check.
- **`gateway/app.py`** takes the `Telemetry` bundle, reads `telemetry.sink` and
  `telemetry.gateway_events`, calls `telemetry.watch_deployments(deploy_bus)`,
  and `await telemetry.stop()` in the lifespan's teardown.
- **`gateway/openai_api.py`** mints the `RequestTrace` before the first thing
  that can refuse, so a 404 is recorded as readily as a 200, and returns the id
  as `X-Request-Id` so a client that saw a bad answer can name the row that
  explains it.
- **`gateway/proxy.py`** calls `sink.request(trace.record(...))` at both settle
  points — one row per attempt, at the moment the numbers exist as themselves.
- **`gateway/internal_api.py`** imports `query as tquery` and serves
  `/api/history/nodes`, `/api/history/requests`, `/api/history/events`,
  `/api/history/logs` and `/api/history/status`.

`GatewayEvents` reaches further without an import. `app.py` builds it once
(`events = telemetry.gateway_events`) and passes it as an `events=` constructor
argument to `AdmissionController`, `CircuitBreaker`, `RetryBudget`, `ParkingLot`
and `Router`, each of which keeps it as `self._events` and null-checks before
every emit — `router.py` is the only caller of `routing_source_failed`.
`restart.py` is the one that reads it off the context instead,
`getattr(self._ctx, "events", None)`, because it is built after
`GatewayContext` and is handed `ctx` whole.

On the registry side, `registry/agent.py` holds a `TelemetrySink` (defaulting to
`NULL_SINK`), samples into it at 1 Hz, records a `shell_opened` event, and
answers `GET /agent/journal` through `journal_payload()` — which returns an empty
payload rather than 404ing when telemetry is off, so a coordinator polling such
a node sees "nothing to collect" instead of a node that looks broken.
`registry/startup.py::_open_telemetry` decides the shape: a coordinator gets
`Telemetry.from_env()`, a worker gets `Telemetry.open(..., coordinator=False)` —
a journal and no archive, because it has nothing to collect from anyone.

```python
from control_plane.telemetry import NULL_SINK, TelemetrySink

class Producer:
    def __init__(self, sink: TelemetrySink = NULL_SINK) -> None:
        self._sink = sink          # never None, never checked at the call site

    def done(self, record) -> None:
        self._sink.request(record)  # returns promptly, never raises
```

`tests/load/driver.py` imports `Hist` from here rather than keeping its own.

## Things that look like details and are not

**One row per attempt, not per client request.** A request that failed over
writes two rows sharing a `request_id`, and the pair *is* the record of the
failover. `RequestRecord.attempts` is how many had been made when that row was
written, which for a failed attempt is not the final total — the authoritative
count is `COUNT(*) GROUP BY request_id`.

**A gap is a row, not an absence.** Both size caps write into `gaps` when they
discard uncollected data, and every history answer carries the gaps overlapping
its window. The UI draws them as hatched stretches under the trace
(`ui/src/tabs/dashboard/chart.ts::gapSpans`) rather than interpolating across
them, because a hole the archive knows it is missing and a machine that was
merely quiet are the same flat line otherwise.

**`events` is the one raw table the archive's size cap never evicts.**
`retention.py::_enforce_size` deletes from `samples`, `requests` and `logs`
only. Sample detail is reconstructible in outline from the rollups; the fact
that a breaker opened is not.

**`retry_reason` keeps `pool_exhausted` apart from `transport`.** It means this
gateway had no free upstream connection, which is not evidence that the backend
was unreachable — and a month later that is the difference between tuning a pool
and replacing a node.

**Telemetry has its own settings and env vars, not the gateway's.**
`config.py` reads `DERATE_TELEMETRY*` directly and `control_plane.paths` for the
root. Nothing here is in the frozen contracts, so a horizon can move without an
announcement — and a telemetry misconfiguration cannot take a gateway setting
down with it.

**`journal_max_bytes()` is a function call, not the bare constant.** The
constant was the default in `Journal.__init__` and nothing ever called the
reader, so `DERATE_TELEMETRY_MAX_BYTES` did nothing at all despite being
documented — and it is the first knob an operator reaches for when a journal is
misbehaving.

## Failure behaviour

- **The data root does not exist.** `Telemetry.from_env` returns
  `disabled("<path> does not exist")` and every producer holds `NULL_SINK`. Not
  an error; it is how `pytest` and a development machine stay clean. Nothing in
  this package creates the root.
- **The data root is not writable.** A `log.warning` and the same disabled
  bundle.
- **The journal will not open.** Caught in `service.py::Telemetry.open`,
  logged, telemetry off for the life of the process. The node still serves.
- **The archive will not open, but the journal did.** Journalling only, with a
  warning: the node keeps its own history and a later coordinator can collect
  it.
- **The in-memory buffer is full.** The oldest queued row is dropped and
  `dropped` moves; it is reported in `stats()` and stored in the `cursors` row.
  The caller is never blocked.
- **A batch fails to commit.** Rolled back and pushed back onto the front of the
  buffer, so a full disk that recovers has not cost the rows written while it
  was full.
- **A node is unreachable.** `poll_once` logs it, records the detail on that
  node's cursor via `note_ship_failure`, and moves to the next source. The
  worker keeps journalling and the coordinator collects the backlog when it
  returns.
- **A journal row will not decode.** Logged at debug and skipped; the rest of
  the page still ingests.
- **A vacuum cannot run** — read-only volume, no room for the rewrite. Warned,
  never raised. Both size loops terminate on *progress* rather than on the file
  measure, so a cap that can no longer be satisfied degrades to a slow trim
  instead of a spin. `MAX_EVICT_BATCHES` (4096) and `MAX_EVICT_DAYS` (400) are
  backstops behind that, not policy.
- **A redactor raises inside the log handler.** The line is dropped. If it
  cannot be proved clean it is not written down.
- **Anything at all raises inside `emit()`.** Swallowed — a telemetry failure
  must never break logging.
- **There is no archive on this node.** `/api/history/*` answers 503 with the
  bundle's own `reason`, except `/api/history/nodes`, which falls back to the
  registry's 300-sample in-RAM ring and labels it `durable: false`.

`tests/unit/test_telemetry.py` (53 tests) gates this package, with
`tests/unit/test_history_api.py` covering the routes above it.

## Deliberately not built

**A second endpoint to acknowledge collection.** Asking for rows after N *is*
the acknowledgement, so there is one endpoint and no ack message. A replayed
request returns the same rows and the archive's primary keys absorb them.

**A push from the node.** Pull reuses the `AgentClient` seam the coordinator
already uses for health, is bounded by an explicit timeout, is testable without
a network, and puts backpressure where the disk is.

**A separate code path for the coordinator's own journal.** It drains through
`LocalSource` into the same `Collector`, so the single-node case exercises the
identical ingest path and is trustworthy before a second machine exists.

**Compaction on reconcile.** `deploy/store.py::purge_expired` is reached only
from `reconcile()`, so a process that stays up never collects. `compact()` runs
every `COMPACT_INTERVAL_S` (60s) whatever else is happening.

**Percentiles averaged from percentiles.** `Hist` merges bucket-wise and exactly,
which is why the rollup tables carry blobs instead of six float columns.

**A level filter standing in for a name filter.** `uvicorn.access` lines are
INFO, so raising the floor would have dropped genuine startup and error lines
alongside them. A logger that is individually cheap and collectively enormous
needs the other axis.
