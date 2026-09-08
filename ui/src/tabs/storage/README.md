# storage

Four cards that answer what the cluster's disks are holding and what can be
taken off them. Every number is measured when somebody asks: `GET /api/storage`
fans out to each node agent and walks a directory on it, and `api/types.ts` says
why there is no alternative — "disk is not in the metrics frame and not in the
archive, deliberately, so nothing here has a history and every number is 'as of
`measured_at`'".

This folder has no tab file and `storage` is not a `Dest`. `SettingsTab.tsx`
imports all four and composes them into its `#st-storage` panel; `StorageTab.tsx`
was deleted in `a58af4b`. Storage stopped being a destination because it is the
same job as Nodes — reading facts off the machines behind this coordinator — and
`SettingsTab`'s own docstring records the move.

## Layout

Table order is the order `SettingsTab` renders them, which is the order they
appear on screen.

| File | Lines | What it owns |
|---|---|---|
| `FilesystemsCard.tsx` | 126 | free space per node per filesystem, and the `severity` → tone mapping |
| `ModelCacheCard.tsx` | 105 | downloaded weights per node, and the only delete in the product that reaches them |
| `EstateCard.tsx` | 167 | what derate itself stores, itemised, plus the retention horizons and the resolver-cache clear |
| `CollectionCard.tsx` | 131 | whether the durable telemetry record is actually durable |

## `FilesystemsCard.tsx`

Capacity where this cluster keeps its data, per node, per filesystem. A
filesystem appears **once** however many of our paths sit on it: the data root,
the resolver cache and the sparkrun cache are usually one disk, and listing them
separately would report the same bytes three times. `fs.mount_paths.join(' · ')`
then names every one of our paths that landed on that device, which is what
shows an operator that two things they think are separate are not.

Free space is the number an operator decides against, so free gets the
`Readout` — `width={6}`, `size="readout"`, unit `GB free` — and `used / (used + free) GB ·
used_pct% used` is the caption beside it, whose denominator is deliberately not
`total`. `tone()` maps `Filesystem['severity']`
(`ok` / `warn` / `critical`, computed server side) onto `ink` / `warn` / `fault`
for both the readout and the `ProportionBar` under it. `fs.reserved` is
captioned only when it is above zero, because `used + free` is short of `total`
by exactly the blocks the filesystem holds back for root — and `used_pct` is
against `used + free`, which is what `df` reports and what a launch can
actually allocate into.

## `ModelCacheCard.tsx`

Downloaded weights on each node, largest first, and the only way in the product
to delete them. These are the biggest files on the machine by orders of
magnitude — the card's own docstring puts a single 120B repository at 182 GB —
and nothing else here can see them. The control plane never downloads them: the
runtime container does, into the host cache it mounts, which is the same
directory the node agent reads for this card.

`servedFolders` is the safety interlock. It takes `useCluster()`'s deployments,
keeps the non-terminal ones (`state !== 'stopped' && state !== 'failed'`,
mirroring `deploy/fsm.py`'s `TERMINAL = frozenset({S.FAILED, S.STOPPED})`), and
maps each through `folderFor` from `api/modelcache.ts`, dropping the bare
`'models--'` a missing `model_id` produces. `CacheTable` renders `serving` in
place of the button for a match. Deleting calls
`backend.deleteCachedModel(nodeId, m.folder)` behind a `window.confirm` that
names the size and says it cannot be undone, then `invalidate()`.

The table itself is `components/CacheTable.tsx` (138 lines), shared with
`tabs/models/InstalledModelsCard.tsx`, which passes neither `onDelete` nor
`servedFolders` and so gets the same rows with no actions column.

## `EstateCard.tsx`

Every file this product writes under its data root, measured, with the horizons
that bound it. The local `bytesLabel` steps GB → MB → KB → B because, in the
file's words, the estate spans four orders of magnitude — "a 69-byte
cluster.json next to a 16 GiB archive" — so a single fixed unit renders most
rows as `0.0`. Rows sort largest first; a component that does not exist keeps
its row at `opacity: 0.45`, so a reader can tell "nothing written yet" from
"this build does not write it".

`cap()` hands a `ProportionBar` to exactly two rows, `archive` and `journal`,
because those are the two keys `RetentionPolicy` actually bounds
(`archive_max_bytes`, `journal_max_bytes`). Every other row is a bare number,
since there is no ceiling to draw it against. The retention block puts seven of
`RetentionPolicy`'s fields through `days()` — raw samples, requests, logs,
events, the 1-minute and hourly rollups, and `journal_retention_s` — and states
the journal's two-sided rule in the last of them: that many days *or*
`journal_max_bytes`, whichever comes first.

The one mutation on the whole screen lives here. `backend.clearResolverCache()`
→ `DELETE /api/storage/cache/resolver`, safe by construction because a cache
miss costs one hub round trip, "which is why it is the only thing offered:
nothing else under the data root can be deleted without losing a record the
product needs".

## `CollectionCard.tsx`

Whether the durable record is actually durable. The card exists for one number,
`dropped`: a node that is dropping journal rows still serves, still reports live
telemetry and still draws a full graph — it has simply stopped keeping the
history, and nothing else in the product would ever say so.

`signal(c)` is the whole judgement. `fault` when `dropped > 0` or `last_error`
is set; `warn` when `behind > 5000` or `age_s > 60`; `live` otherwise.
Collection runs every few seconds, so small backlogs are the normal state and
reporting them would train an operator to ignore the lamp. The header line says
the same thing in words: a node that is behind has not lost anything yet, a node
that has dropped rows has. `dropped` is additionally coloured `var(--fault)` in
its own cell, and each node's `last_error` is rendered under the table through
`Verbatim`.

Telemetry switched off is a different card body, not an empty table: it says the
graphs are whatever this browser has accumulated since it loaded, and prints
`telemetry.reason` verbatim. The footer carries archive bytes, the four row
counts, the oldest sample, and this node's own journal size and queue depth.

## The seam with `SettingsTab`

`SettingsTab.tsx` is the only importer:

```tsx
<div id="st-storage" role="tabpanel" aria-labelledby="st-tab-storage" hidden={sub !== 'storage'}>
  <FilesystemsCard />
  <ModelCacheCard />
  <EstateCard />
  <CollectionCard />
</div>
```

Everything else is read through hooks rather than props — no card takes an
argument.

- **`useStorage()`** (`state/resources.ts`, 30 s) is the single data source for
  all four. Its comment is explicit about why it is slow on purpose: one call
  fans out to every node agent and walks a directory on each, "and the number it
  returns moves over hours, not seconds. There is no stream to fall back on —
  disk is not sampled anywhere".
- **`useCluster()`** (5 s), only in `ModelCacheCard`, and only to build
  `servedFolders`.
- **`useBackend()`** for the two mutations and for `invalidate()`, which bumps
  the provider's `revision` so every polled resource refetches immediately
  instead of waiting out its interval.
- **Shared components:** `Lamp`, `Verbatim`, `ProportionBar`, `Readout`,
  `CacheTable`; `gbytes` and `relativeTime` from `format.ts`; `folderFor` from
  `api/modelcache.ts`.
- **Types**, all from `api/types.ts`: `StorageReport`, `NodeStorage`,
  `Filesystem`, `EstateEntry`, `UnreadablePath`, `CachedModel`, `ModelCache`,
  `CollectorCursor`, `JournalStats`, `ArchiveStats`, `TelemetryEstate`,
  `RetentionPolicy`, `CacheClearResult`, `ModelDeleteResult`.

The routes behind them are `GET /api/storage`,
`DELETE /api/storage/nodes/{node_id}/models/{folder}` and
`DELETE /api/storage/cache/resolver`, all in `gateway/internal_api.py`.

## Things that look like details and are not

**`available: false` is never rendered as a number, in any of the three places
it can appear.** `FilesystemsCard`'s `NodeRow` returns early with a dash and the
server's sentence through `Verbatim` — its comment says why: "a 0 here reads as
'full', and a dash plus the server's sentence is the only honest rendering of
'we could not look'". `EstateCard` filters the node list on `n.available` before it
renders a row. `CacheTable` prints `not measured` and the cache's own
`reason`. The same rule governs `EstateEntry.bytes`, which is `null` — never 0 —
when a component does not exist or could not be read, and `bytesLabel(null)` is
an em dash.

**The in-use check compares encoded folder names, never decoded repository
ids.** `ModelCacheCard` maps every live deployment's `model_id` through
`folderFor` and compares folders; `internal_api.py` does the identical thing
server side before refusing with a 409. Decoding is the direction that does not
work: `api/modelcache.ts` records that `models--a--b--c` could be `a/b--c` or
`a--b/c`, and `repoIdIsUnambiguous` exists because re-encoding a decoded id and
comparing is a tautology rather than a check — both sides split on the first
separator, so it holds for every folder there is, ambiguous ones included.

**Withholding the button is not the enforcement.** The server refuses a delete
for a served model regardless, with a 409 carrying `model_in_use` and a sentence
naming the deployment. `internal_api.py` puts the check on the coordinator
deliberately: "The agent has no idea what a deployment is". `CacheTable`'s
comment on the `serving` marker says the same from the other end — marked, not
merely disabled, because the reason is the useful half.

**Each card carries its own byte formatter, and the two are not one function
under two names.** `EstateCard` and `CollectionCard` call theirs `bytesLabel`;
`ModelCacheCard` and `CacheTable` call theirs `sizeLabel`, and grepping for the
wrong one finds nothing. `EstateCard`'s is the only one that steps down to raw
bytes, because a `cluster.json` is 69 of them; `CollectionCard`'s stops at KB
and gives GB two decimals; both `sizeLabel`s stop at KB with one decimal at GB,
where the smallest interesting object is a blob. `format.ts`'s `gbytes()` is not
a fourth: it divides by 1024³ and returns a bare number with no unit, so every
caller appends its own `GB` — `FilesystemsCard`'s captions and its readout
`title`, and the GB branch inside `EstateCard`'s `bytesLabel`. The `Readout`
itself does not call it; it is handed `fs.free / 1024 ** 3` with
`decimals={0}`.

**All four cards call `useStorage()`, and `useResource` does not deduplicate.**
`state/resources.ts` states that plainly at `useActivity`, and
`useKeyedResource` holds its state per component with no shared cache. Because
`SettingsTab` keeps every panel mounted and hides it with `hidden` rather than
unmounting — enrollment tokens and half-typed addresses must survive a tab
switch — the cluster-wide disk walk starts when `/settings` loads, not when the
Storage sub-tab is opened.

**The estate is a declared list, not a directory listing.**
`registry/storage.py`'s `ESTATE` names thirteen components and every one is
owned by a module in this repo. `/data/sparkrun-cache` is excluded on purpose:
`entrypoint.sh` symlinks it to `$HOME/.cache/sparkrun`, and following it would
attribute another tool's bytes to ours. Its *filesystem* is still measured,
because the symlink target is one of the probe paths — which is exactly the case
`FilesystemsCard`'s one-row-per-device rule exists to keep honest.

## Failure behaviour

- **Reading.** Each card renders `Measuring…` while `storage.loading`.
- **The read failed.** Only `FilesystemsCard` surfaces `storage.error.message`,
  in `var(--fault)` with `white-space: pre-wrap`. It is the first card on the
  panel; the other three fall through to their empty states, which is why a
  failed fetch does not look like four separate problems.
- **Nothing to show.** `No nodes in the cluster.` (Filesystems), `No node
  reported a data root.` (Estate, after the `available` filter), `No node
  reported a model cache.` (Model cache, when no node carries `models`).
- **A node could not be read.** Its id plus the server's `reason` through
  `Verbatim`. Never a zero.
- **Individual paths could not be read.** `node.unreadable` renders one line per
  path in `var(--warn)`, each carrying the server's reason verbatim.
- **A filesystem list came back empty on an available node.** `no filesystem
  could be measured` — distinct from the node being unavailable.
- **Telemetry is off.** `CollectionCard` replaces its table with the explanation
  and `telemetry.reason`.
- **A delete or a clear was refused.** The server's message goes into the card's
  red `label` block with `white-space: pre-wrap`, unshortened. The delete path
  has five refusals to carry — 409 `model_in_use`, 503
  `cluster_token_unavailable`, 404 `node_agent_unreachable` when the node has no
  agent URL, 502 `node_agent_unreachable` when the agent did not answer, and
  `delete_refused` when the agent answered and said no, which forwards the
  agent's own status code except that a 403 is rewritten to 502 — and the card
  never paraphrases which one it got. The clear path has two of its own: 503
  `resolver_cache_unavailable` when the resolver has no shape cache, and 500
  `resolver_cache_clear_failed` when `cache.clear` raised.
- **A delete succeeded.** `bytes_freed` is reported and `invalidate()` refires
  every polled resource, so the table redraws from a new measurement rather than
  from a local edit to the old one. The two cards place it differently:
  `EstateCard` puts `<bytes> freed` beside its own button, while
  `ModelCacheCard` puts `<size> freed from <node>` under the tables, because the
  button that started it is one row inside one of them.

## Deliberately not built

**No history, and no sampling.** Disk is read on demand and appears in neither
the metrics frame nor the archive, so no card here can draw a trend. That is
recorded in `api/types.ts` above the storage types and repeated in
`useStorage`'s comment: the number moves over hours, and one call already costs
a directory walk on every node.

**No second destination.** Storage is a sub-tab of Settings rather than a
`Dest`, because it is the same job as Nodes. `SettingsTab`'s docstring notes the
four cards moved in unchanged, docstrings included.

**No delete for anything else under the data root.** The resolver cache is the
only offer on this screen, and `EstateCard` says why: nothing else there can be
removed without losing a record the product needs. A ceiling is drawn for the
archive and the journal precisely because retention already deletes those two
without anybody pressing a button.

**No verifier in this folder.** Twenty-one `*.check.mjs` files sit under
`ui/src` — in `api/`, `shell/`, `sidebar/`, `state/` and seven folders under
`tabs/`, two of them in the sibling `tabs/settings/` — and none of them is here.
These cards reach a browser only through `shell/screens.check.mjs`, which walks
`routes.ts`'s `DESTINATIONS` (derived from `SEGMENT`, never a hand-kept list).
`/settings` is a destination and `SettingsTab` opens on
`useState<Sub>('connection')`, so the four cards are mounted and hidden in that
capture — typechecked and screenshot-adjacent, not asserted on.
