# inventory

One record per model, folded on the server from five sources, so the Models
screen fetches one shape instead of six. The browser used to hold this merge --
six endpoints, folded in the tab itself, while fourteen other screens each
re-derived their own answer -- so a model that was curated, already on disk
*and* serving right now arrived as three rows, each saying a third of it.

**It is a materialized view, and it owns nothing.** A deployment's state still
belongs to `DeploymentManager` and `deployments/*.json`; a provider's models
still belong to `providers.json`. What lives here is the merge. Nothing routes
off it: `/v1/chat/completions` and `/v1/models` go through the live
`TargetIndex`, because a stale row that sent traffic to a dead backend, or that
promised a name the router had already dropped, is a far worse bug than a stale
screen.

Neither refresh path patches rows. Both are full re-reads of the owning store,
so the worst reachable state is a stale row with an honest timestamp on it --
never a wrong row that no re-read will correct.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `service.py` | 674 | `ModelInventory`: the two refresh paths, the digests, and every read |
| `db.py` | 249 | the SQLite schema, the connection, and `SCHEMA_VERSION` |
| `api.py` | 235 | `GET /api/models`, and the payload the screen already reads |
| `build.py` | 188 | the pure merge -- deployments, curated and providers on `model_id` |
| `records.py` | 153 | `ModelRecord`, the three row types it carries, and `CacheScan` |
| `__init__.py` | 37 | the export surface: ten names, and the only import path |

## `service.py`

`ModelInventory` owns the database and every entry point takes its lock. Two
refresh paths, because the facts they carry cost wildly different things.
`refresh_fast(deployments=, provider_facts=, catalogues=, curated=, errors=)`
reads what is already in memory on the coordinator and can run whenever
something changes. `refresh_cache(report)` takes the weights-on-disk half out of
a `GET /api/storage` payload. Each stamps when it last ran and the endpoint says
so, because a screen presenting a five-minute-old disk figure as current tells a
small lie every five minutes.

Reads are `list_models()`, `cache_scans()`, `sources()` and the `revision`
property. `drop_nodes(keep)` forgets nodes that left the roster, and is called
only with a roster the caller actually read -- if `list_nodes()` raised, doing
nothing at all is correct, because a registry hiccup must never wipe the disk
picture for the whole cluster.

**Both refreshes short-circuit on a digest, and the digest is why the revision
counter means anything.** `_digest` hashes exactly what the fast tables store
and nothing that moves on its own; `_cache_digest` deliberately excludes
`measured_at`, which changes on every fan-out whether or not a byte moved on any
disk. Six screens poll `/api/storage` on a 30s timer and every one of them feeds
`refresh_cache`, so without the short-circuit the cache rows were deleted and
reinserted every two or three seconds and the revision counter -- which exists
so a reader can tell a change from a re-poll -- climbed past 250 in five minutes
and meant nothing. A re-poll still moves `attempted_at`, in two `UPDATE`s
rather than a rewrite of every row, and deliberately does not bump the revision.

**A partial read is never written as though it were the whole picture.** When
`errors` names a failed feed, `refresh_fast` records what happened in `sources`
and leaves the tables alone -- writing the merge would delete every row the
failed feed owns. `refresh_cache` applies the same rule per node: a node whose
agent did not answer keeps its `model_cache` rows and lands in `cache_scans`
with `available=0` and a sentence.

`list_models()` computes the `ondisk` facet at read time rather than storing it,
because it comes from the slow refresh while the fast one rewrites
`model_facets` wholesale. A model that is *only* on disk -- somebody pulled it
by hand -- has no row in `models` and gets one synthesised.

## `db.py`

One database at `<data_dir>/models.db`, one table per kind of fact. The idiom is
transcribed from `telemetry/archive.py` rather than invented: WAL,
`isolation_level=None` with explicit transactions, `busy_timeout=30000` so a
concurrent reader never surfaces as an error, and a `meta` table stamped with
`SCHEMA_VERSION`. `connect()` turns `check_same_thread` off because the refresh
runs through `asyncio.to_thread` and lands on whichever pool thread is free;
`ModelInventory`'s lock is what keeps one thread on the connection at a time.

**`PRAGMA auto_vacuum=INCREMENTAL` is issued before `journal_mode=WAL`, and the
order is not cosmetic.** WAL fixes the page size, and SQLite then refuses an
auto_vacuum change without saying so -- the same ordering mistake in
`telemetry/journal.py::_connect` cost a coordinator its entire telemetry record.

`SCHEMA_VERSION` is 1 and a bump discards the file. `open_database()` reads the
stored version, drops every table when it disagrees, and logs that it is
rebuilding from the live sources. Every row here is derived, so there is nothing
to migrate and nothing is lost by starting over. `FAST_TABLES` names the five
tables `refresh_fast` rewrites wholesale in one transaction.

Why a database and not another JSON store: `providers.json` is 150 KB rewritten
in full on every mutation, which is fine for a file one service owns. This holds
the union of five sources, has one writer and every screen as a reader, and the
interesting questions about it are joins -- which models does this node hold,
which are served and by whom, which are published and switched off. Those are
questions a table answers and a document does not.

## `api.py`

`GET /api/models`. The router lives here rather than in `control_plane/gateway/`
so that what lands in a file somebody else owns is wiring and not logic: in
`app.py`, one import at module scope, one guarded construction and one
`include_router` call. It reads the ports off the gateway context and nothing
else.

**One literal path, and deliberately no `/api/models/{model_id}`.**
`capacity_api` already owns `/api/models/quant-table`, `/search`, `/detail` and
`/variants`. A path-parameter sibling would shadow all four depending on
`include_router` order in `create_app` -- the kind of breakage that arrives
months later when somebody reorders the wiring for an unrelated reason. A single
model is `/api/models?model_id=...`, and `?where=` filters by facet on a
comma-separated list.

`refresh_from_ports(inventory, deps, curated=)` reads each feed under its own
guard, in the `_safe` posture `Router.rebuild` already uses: a port that raises
degrades *that feed only*, and the sentence travels to the screen in `sources`.
`public_list` and `catalogue` are duck-typed and not on the frozen
`ProviderPort`, so a port carrying only the protocol contributes no provider
rows rather than raising. `FAST_TTL_S` is 2.0 seconds; the sources are
in-memory, so the only cost of asking again is the merge, and the digest
short-circuit means an unchanged one writes nothing.

`record_payload` splits `providers` and `offers` back apart even though the
database holds them in one table with a flag. Both are right: the table is what
makes "served" and "merely published" one decision taken once, and the two
arrays are the distinction the pane draws -- a single list would let a Stop
serving button appear beside four hundred models nobody chose. `_offer_payload`
carries no health and no admission fields, because nothing is routing to an
un-served model and a health figure there would describe a path that does not
exist.

## `build.py`

The merge, pure given its inputs, and the Python port of
`ui/src/tabs/models/rows.ts`'s five builders and `mergeRows`. Free of I/O and of
the database for the reason `gateway/targets.py::build_index` is: that is what
makes a merge testable.

**The key is `model_id`, exact, never case-folded.** OpenRouter spells a model
`qwen/qwen3-30b-a3b` where the hub spells it `Qwen/Qwen3-30B-A3B`, and folding
the two together would hand an un-served remote row a local verdict for weights
that are not the same thing.

`build()` runs `_add_running`, `_add_curated`, `_add_providers` in that order --
the order the browser folded them in, local and structured facts first. `_touch`
keeps the first label it was given unless a curated one arrives, because a human
wrote that one and the sort is on the label. `_add_running` keeps FAILED and
STOPPED deployments: a finished deployment is still the answer to "what happened
to this model". `_add_providers` reads `enabled` off each catalogue row -- true
is the `provider` facet, false is `offered` -- and an un-served model claims no
served name, because nothing answers to it at `/v1` and listing it would make
the row searchable by a name that routes nowhere.

The `ondisk` facet is absent from everything here on purpose: weights on disk
cost a fan-out and refresh on their own slower cadence, so they live in their
own tables and are joined at read time.

## `records.py`

`ModelRecord` is the server-side twin of the UI's `ModelRow` minus the fit
fields, and it carries three row types: `RowDeployment` (plural on the record,
because a repository can be served twice under two names), `RowProvider` and
`RowCache`. `CacheScan` is the fourth dataclass here and the one that is not
about a model at all -- it answers whether one node's cache could be read.
There is no `total_params`, `native_dtype`, `downloads`, `likes` or `tags` --
the first two are produced by the fit gate's capacity walk and the rest by a
HuggingFace search, and neither is one of the five things being folded.

`FACET_ORDER` is the six facets in canonical order -- `running`, `ondisk`,
`catalog`, `provider`, `offered`, `hub` -- and `SERVER_FACETS` is that set minus
`hub`, which this process never emits. `RowProvider` carries `served` and
`provider_enabled` as separate answers, and carries nothing key-shaped:
`api_key_ref` is a name, not a value, and it is not here either.

**`bytes_on_disk` is the largest figure any node reports, never the sum.** Two
node records can share one physical cache -- this cluster registers a node and a
probe worker on the same host, both reporting the same repositories -- and
summing would claim double the size for a single download. How many nodes hold
it is `cached_on`, carried separately.

## `__init__.py`

The import path, and the ten names in `__all__`: `ModelInventory`,
`ModelRecord`, `build`, `SCHEMA_VERSION`, `FACET_ORDER`, `SERVER_FACETS`,
`CacheScan`, `RowCache`, `RowDeployment` and `RowProvider`. Its docstring states
both rules the rest of the package rests on -- the merge happens once, on the
server; the stores it reads from keep owning their records.

## The seam with the gateway

`control_plane/gateway/app.py` is the only module in `control_plane/` that
imports this package, and it does so on one line at module scope
(`from control_plane.inventory import api as inventory_api`) plus a guarded
construction inside `create_app`:

```python
try:
    from control_plane.inventory import ModelInventory
    from control_plane.paths import data_path

    ctx.inventory = ModelInventory(data_path("models.db"))
except Exception:
    log.exception("model registry unavailable; /api/models will say so")
```

`GatewayContext.inventory` in `gateway/deps.py` defaults to `None` for that
reason: every existing construction keeps working, and a coordinator that could
not open the database still serves.

- **`app.include_router(inventory_api.create_router(ctx))`** is registered above
  the `StaticFiles` mount, like every other router -- a Starlette mount at `"/"`
  catches every path not matched by an *earlier* route, so below it this would
  answer `index.html` to a fetch expecting JSON. It is also registered *below*
  `capacity_api` on purpose, belt and braces over the four literal paths that
  router owns under `/api/models/`.
- **`gateway/internal_api.py`** feeds the slow half. `GET /api/storage` already
  fans out to every node agent, so it hands its own payload to
  `inventory.refresh_cache` and then `inventory.drop_nodes(keep)`, both through
  `asyncio.to_thread`, guarded, and deliberately after its own response body is
  built. The direction is one-way -- storage feeds the registry, never the
  reverse -- or a registry refreshed on a timer would start answering the
  Storage tab with a reading older than the one it just took.
- **`control_plane/fit/catalog.py`** supplies `CURATED_MODELS`, imported lazily
  inside `refresh_from_ports` and overridable by its `curated=` argument.
- **The UI** calls `api.modelRegistry()` in `ui/src/api/client.ts`, turns the
  payload into rows with `registryRows` in `ui/src/tabs/models/rows.ts`, and
  folds hub search hits on afterwards with `withHubHits` -- the only half of the
  old `mergeRows` that survives, because `/api/models/search` resolves nothing
  and its hits are a query's answer rather than a fact about this cluster.
  `ui/src/tabs/models/registry.check.mjs` (`// requires: coordinator`) checks
  that boundary, where a source silently dropping out of the server-side merge
  is invisible on a screen that still looks full.

## Things that look like details and are not

**`served` and `provider_enabled` are two questions, and they disagree more
often than they look.** `ProviderService.servable()` filters by the allowlist and
deliberately does not filter by `provider.enabled` -- `build_index` does that
separately -- so an allowlisted model on a disabled provider is listed by
`/api/providers` today while nothing routes to it. Carrying both columns means a
reader can tell "served" from "would be served if the provider were on" instead
of inheriting that ambiguity.

**`model_provider_rows` is one table with a flag because two endpoints were two
answers.** The screen draws "served by" and "published, switched off" as
separate lists and used to build them from two endpoints polled at different
intervals, so for one poll after somebody enabled a model a row claimed both and
the pane drew a Serve button beside a Stop serving one.

**`sources` is load-bearing, not decorative.** The Models tab's rule is that a
feed failing greys nothing and empties nothing -- it prints one line naming the
feed and the server's own sentence. Folding five fetches into one leaves the
browser with no failed request to notice, so the sentence has to travel in the
payload or it stops existing. `FAST_SOURCES` is `deployments`, `providers`,
`catalog`; `cache` and `store` are added on the way out.

**`observed_at` and `attempted_at` are different columns and both are needed.**
`observed_at` is the last *successful* read, `attempted_at` the last try. They
move independently so a screen can say "still asking, last answer 40 minutes
ago", and `observed_at IS NULL` -- never measured -- is a third answer again and
is never rendered as a zero. On a feed that failed, `_note_sources` leaves
`observed_at` where it was: the rows that feed owns are still standing, and
saying they were read just now would be a lie about the only number that says
how stale they are.

**A cache directory with `blob_count == 0` is skipped, exactly, never on a size
threshold.** It is a resolve that touched the repo and wrote nothing, and
counting it would say "already downloaded" about a model of which not one byte
is present.

**`blob_count` is carried instead of a `complete` verdict.** Whether what is on
disk is the whole download needs an expected size, and only the variant ladder
ever knows one. A base repository has nothing to check against, and guessing
there would call a finished download partial.

**No fit verdict and no credential field ever reaches a row.** A verdict is an
answer to a question -- (model, context, concurrency, node set) -- and a row
here carrying one would be asserting a verdict nobody asked for; it stays on
`/api/capacity` and the UI joins it on. `tests/unit/test_inventory_api.py` asks for
both structurally, over field *names*: a substring search for a key would miss
`api_key: '***'` and would also fire on a payload behaving correctly, since a
provider whose key does not resolve reports a `last_error` naming the reference,
and that sentence is the product telling an operator what to fix.

## Failure behaviour

- **The database will not open.** `ModelInventory._open` logs, unlinks the file
  and its `-wal` and `-shm` siblings, and reopens. Nothing in here is anything
  but derived, so discarding is the recovery; a corrupt file must never lock an
  operator out of a running cluster.
- **The schema version moved.** `open_database` drops every table and reapplies
  the schema, logging what it found and what this build wants. The next refresh,
  seconds later, rebuilds it.
- **`create_app` could not construct the registry at all.** `ctx.inventory`
  stays `None` and `GET /api/models` answers 200 with an empty `models` list and
  `sources.store.reason` reading "This coordinator has no model registry."
- **One feed did not answer.** That feed's rows stand, `sources[feed].ok` is
  false with the port's own sentence, and its `observed_at` does not move.
- **A node agent did not answer.** Its `model_cache` rows stand, `cache_scans`
  records `available=False` with the reason, `attempted_at` moves and
  `observed_at` does not. Deleting the rows would turn one unreachable worker
  into a confident claim that nothing is downloaded anywhere.
- **The refresh raised inside the request.** Logged with `log.exception` and the
  handler answers from the store. A stale answer with honest timestamps beats a
  500 over a view that can be rebuilt.
- **A read raised.** `records`, `sources` and `revision` degrade to empty and
  `sources.store` carries "The model registry could not be read: ...".
- **Any write raised mid-transaction.** `ROLLBACK`, then re-raise. Every write
  path here is `BEGIN IMMEDIATE` / `COMMIT` with that rollback, so a half-written
  merge is not a reachable state.

`tests/unit/test_inventory.py` (24 tests) gates the merge and the ways it could
quietly lie -- a source dropping out, an unreachable node reading as an empty
disk, a verdict appearing for a question nobody asked -- and
`tests/unit/test_inventory_api.py` (9 tests) gates the wire, including that the route
is reachable with the UI mounted and that it does not shadow `capacity_api`'s
four literal paths.

## Deliberately not built

**`/api/models/{model_id}`.** It would shadow four working paths on an
`include_router` reorder. `?model_id=` filters the list, and
`/api/models/detail` already answers the resolver's version of that question.

**A fan-out of its own.** Walking every node's cache is the expensive part of
`/api/storage`, and scheduling a second cluster-wide walk to populate
`/api/models` would double it for the same answer. `refresh_cache` is fed, not
fetched.

**The `hub` facet.** `FACET_ORDER` lists it so the ordering lives in one place,
but this process never emits it: HuggingFace search is keystroke-driven, resolves
nothing, and stays its own endpoint. The browser adds that facet in
`withHubHits`.

**Columns for what the screen renders but no source here knows.**
`total_params`, `native_dtype`, `downloads`, `likes` and `tags` would be NULL on
every row forever, and worse than useless: `rows.check.mjs` fails a row carrying
`total_params` without a verdict to have got it from, which is exactly what
filling one in from here would produce.
