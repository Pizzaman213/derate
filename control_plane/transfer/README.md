# transfer

Copies a HuggingFace cache folder (`models--owner--name`) from the
coordinator's cache to another node's, over the fabric when the two share a
subnet on it. A launch calls it before sparkrun (`deploy/manager.py::_prestage`),
Settings → Downloaded models offers it per node (`POST /api/transfers`), and
background copies propose it ahead of time (below), running only on approval.

sparkrun distributes weights with `rsync -e ssh`, and one ssh stream holds a
100 Gb/s rail to ~1.3 GB/s because of its cipher. This module runs N plain
TCP streams spread across the fastest links the two nodes share, `sendfile` on
the sending side, and an authenticated session on the receiving side.

## Which links a copy uses

Every physical interface with carrier is a candidate, whatever its vendor
(`/sys/class/net/<if>/device`), with its speed (`.../speed`) and whether it can
do RDMA (`.../device/infiniband/`). An IPv4 or global IPv6 address pairs with a
remote one in the same subnet; an IPv6 link-local address pairs with any
remote link-local one, which is what makes a bare cable usable with no netplan
at all. Each candidate is probed with one TCP connect (2 s), and the copy uses
the fastest tier that answers, each interface once, rail paired with rail.
`path` is `fabric` (all RDMA-capable), `network` (the fastest other link) or
`management` (nothing answered: the agent's own address); `link` is what the
screen shows ("RoCE, 2 × 200 Gb/s"). On a link of 10 Gb/s or less, or of
unknown speed, a copy uses at most 2 streams. The receiver listens dual-stack
on `::` (IPv4-only where IPv6 is off).

## Pieces

| File | What it is |
|---|---|
| `fabric.py` | `link_addresses()`: every address on a lit physical port, with speed and RDMA. `candidate_paths()`, `fastest_tier()`, `describe()`: which pairings to try and which to keep. |
| `protocol.py` | Constants, the manifest and its validation, the handshake MAC, blob digests. |
| `receiver.py` | Runs on every node agent, on `TRANSFER_PORT` (47101). |
| `sender.py` | `send_folder()`: one folder to one node. |
| `jobs.py` | `Replicator`: the coordinator's copies in flight, shaped like `deploy/prefetch.py::Prefetcher`. |
| `replication.py` | Background copies: `ReplicaStore`, the pure `plan_replication`, `ReplicationMonitor`. |

## Background copies

`ReplicationMonitor` re-plans every 30 s. A model is wanted on a node when a
deployment that unloads when idle or starts at boot has that node in its plan,
then when the model is pinned (every other node). Each wanted copy becomes a
**proposal** (bytes to send, free space, reasons, and every one of derate's own
copies it would delete to stay above that node's free-space floor) or one
sentence saying why not: absent or still downloading on the coordinator, node
unhealthy, node on an older build, a PINNED model's weights larger than the
node's memory (a deployment's plan already fits across its nodes, so its wants
skip that check), dismissed, or no room even after deletions.

The bytes charged are the whole checkpoint unless the overlap is proven: only a
partial copy of the coordinator's own commit (the node lists that snapshot, or
derate's record of the copy names it) subtracts what is there. A copy at another
commit, or one nobody can name, may share no blobs and keep its old ones.

The floor is per node: `node_floor` = the smaller of `REPLICA_FREE_FLOOR`
(100 GiB) and a fifth of the node's cache filesystem, which the agent reports
as `total` on `/agent/transfers/capacity`. A 256 GiB disk keeps 51.2 GiB free, a
4 TB disk 100 GiB; an agent that sends no `total` gets the 100 GiB cap. Each
wanted folder is planned against `?folder=`'s answer -- the filesystem the
receiver would land THAT folder on (`landing`) -- and only derate's copies on
that same filesystem count as room; an agent that answers 400/404 or no
`landing` is planned on its folder-less answer. Each
proposal and each floor-refused skip carries its own `floor_bytes`; the
top-level `floor_bytes` of `GET /api/replication` is the cap.

**Nothing is copied or deleted without an approval** (`POST
/api/replication/approve`), and the approval must name the exact deletions the
operator was shown (409 otherwise). The approval also records the coordinator's
revision and size, the target and its floor; if any of those differs when the
copy would start, the approval is withdrawn with a `last_error`. A plan that
later needs a NEW deletion withdraws it too; fewer deletions than approved stay
covered, which is how a paused copy resumes on its original approval. A failed
copy drops its approval and shows `last_error`. Dismissing hides a proposal
until the coordinator's copy changes size or revision. A launch never cancels a
copy that is verifying (`CancelRefused`); it finishes and is recorded.

An approved copy starts only while nothing is launching and no other copy
runs. A launch cancels a background copy unless the launch needs that very
copy. The receiver moves each blob into place as soon as it hashes to its name,
so a cancelled copy keeps what arrived and the next run sends only the rest.
A copy is recorded as derate's when it starts (`complete: false`), so a half
copy can still be cleaned up. Deletions only ever take derate's own copies that
nothing still wants (a still-pinned copy deleted for another would be proposed
straight back) and that no live deployment uses, oldest-used first.

Live run on the two Sparks (throwaway receiver container, own cache dir):
proposal held three passes unapproved; Qwen3-4B approved and copied; Qwen3-30B
refused until its deletion was named, then deleted 4B and copied; a launch
paused it with 26.1 GiB of verified blobs kept; it resumed on the same approval
and finished. On the management path copies use at most 2 streams.

## The protocol

The manifest is opened over the agent's token-gated HTTP API
(`POST /agent/transfers`). The answer is a session id, a 32-byte secret, the
receiver's fabric addresses, and the indices of the content-addressed blobs it
already holds at the same size (rsync's `--size-only` rule, applied only where
the name is the hash). Every other file -- `refs/main` is 40 characters for any
commit, `trees/*`, the `.no_exist` markers -- is always sent, or a revision
refresh would leave the node on the old commit. Each raw stream
then sends `MAGIC` and the session id, receives a nonce, proves the secret with
an HMAC over it, and pushes `(file_index, offset, length)` chunks, each acked
with one byte. `POST /agent/transfers/{id}/commit` checks every byte arrived,
renames the partials into place, and restores modes, mtimes and links.
`DELETE` discards a session and its partials.

Nothing on the wire places a byte: a chunk names a file by its index in a
manifest that was validated on open (relative paths under `blobs`, `snapshots`,
`refs`, `.no_exist` or `trees`; links that resolve inside the folder).

**Blobs are hashed against their names.** `blobs/<64 hex>` is sha256,
`blobs/<40 hex>` is git's blob sha1. Each file is fsynced and hashed the moment
its last chunk lands, while the rest still stream, so commit waits only for
the tail. A mismatch refuses the whole copy with 422 and removes it. A file's
write descriptor is closed the moment its last chunk lands and no stream holds
it, so a copy of thousands of files holds open only the ones still arriving,
however far the checks fall behind; the check reopens the partial read-only
through the session's directory descriptors with `O_NOFOLLOW`. A resent chunk
of a file already complete (the sender lost an ack) is read and dropped, and
acked only while the copy is open and that file's check has not failed -- after
a failed hash it is refused, never acknowledged as landed.

**The handshake has 10 s.** Until a stream has proved the secret it holds one
of the 32 stream slots, so hello and MAC share one 10 s deadline
(`_AUTH_TIMEOUT_S`); the 120 s I/O timeout starts only after `ACK_OK`.

**Writes are positional.** Streams of one file share a descriptor and write
with `os.pwrite`, so one stream never lands another stream's bytes.

**Ownership and times are carried over.** The agent container runs as root and
sparkrun's rsync runs as the user, so everything created is chowned to the
cache directory's owner; mode and mtime are restored so that rsync finds
nothing to send.

**No link under the cache folder is followed.** Root writes into a folder the
user owns, so a symlink planted in it (`blobs -> /etc`) would otherwise be
written, chmodded and chowned through. A session opens its landing directory
(the hub, or the resolved extra folder) once, and every name below it is
reached relative to an open directory descriptor with `O_NOFOLLOW`: a link or
file where a folder belongs refuses the copy with 409 (on open, or at the
first chunk if it appears later); a link where a file belongs is not counted as
held and is replaced by the real file; a partial is always created fresh with
`O_EXCL`; mode, mtime and owner are set on the descriptor before the rename.
A platform without directory-relative, no-follow operations (`dir_fd`,
`O_NOFOLLOW`, `O_DIRECTORY` -- Windows) does not run the receiver at all:
`start_transfer_receiver` raises and the agent logs why, because a path-check
fallback is a race against whoever else can write the cache.

**A link out of the folder travels as its file.** huggingface_hub 1.x lands a
xet blob as a symlink into a shared `hub/blobs/` store; the receiver has no
such store, so the sender sends the content.

## Extra cache folders (`DERATE_EXTRA_CACHE_DIRS`)

exo's `EXO_MODELS_DIRS` idea -- the first folder with room wins -- under the one
constraint derate cannot move: a launched container mounts exactly the HF cache
root (`~/.cache/huggingface` -> `/cache/huggingface`) and the recipe format has no
`volumes:` key. So an extra folder must resolve beneath that root (an SSD mounted
at `~/.cache/huggingface/extra-ssd`; `-v` is a recursive bind, so the container
sees the mount). Anything else -- relative, missing, outside the root, through a
symlink that leaves it, or inside `hub/` -- is refused with a sentence logged when
the receiver starts and returned as `refused` by `/agent/transfers/capacity`.

On open, a folder already in `hub/` (or already linked) is filled where it is,
never split. A new one lands in `hub/` when it has room under the 10 GiB reserve,
otherwise in the first extra folder with room (one already holding a partial
copy first). Commit then makes `hub/models--X` a **relative** symlink to
`<extra>/models--X`; an absolute link names a host path the container lacks.
`modelcache.linked_folder` is what recognises that link as derate's own: the
scan measures through it, delete removes both the folder and the link (bounded
to a direct child of a configured extra folder), and the receiver fills it. Any
other symlink is still ignored by the scan, refused by delete, and refused on
open with "`<folder>` is a symlink here".

`/agent/transfers/capacity` reports `free`/`total` of the roomiest folder, so the
background-copy planner's floor is checked against where the copy can land, plus
`dirs` (each folder's free/total) and `refused`. With `?folder=models--X` (and
`&bytes=`, the copy's size) `free`/`total` are instead the filesystem that
folder would actually land on, and `landing` names its directory (a
`dirs[].path`); a bad name is 400. Both answer through one function
(`receiver._select`), so they cannot pick different folders: `open` credits the
blobs it finds in a candidate against the copy, and `capacity` credits the bytes
of regular files already in it -- so an extra folder holding an interrupted
copy is named when only the remainder fits there. An older agent ignores both
parameters and sends no `landing`.

Proved on spark-26af, 2026-11-21, with throwaway temp trees:

- `docker run --user 1000:1000 -v <hf_root>:/cache/huggingface` read and wrote
  `hub/models--x/blobs/*` through `hub/models--x -> ../extra/models--x`; an
  absolute link to the same folder read `No such file or directory`; and
  `-v /run/user:/x` showed the separate tmpfs at `/x/1000`, so a disk mounted
  beneath the root is inside the container.
- sparkrun's own command shape, `rsync -a --size-only --mkpath --partial --links
  <src>/models--x/ <dst>/hub/models--x` (and the `model_distribute.sh` form with a
  trailing slash), against a destination whose `models--x` is our symlink: rsync
  enters the named destination folder, so the link survives, a pre-staged blob is
  not sent again, and a missing blob is written into the extra folder. Syncing the
  PARENT (`<src>/ <dst>/hub/`) would replace the link with a real directory and
  refill the hub disk -- sparkrun never does that (`models/distribute.py`
  `run_rsync_parallel(model_cache_path(...))`), but a change there must be checked.

## Measured on the two Sparks, 2026-11-21

Rail addresses were added temporarily (10.100.152.2/24, 10.100.153.2/24) and
MTU was 1500 on the peer; the receiver ran in a throwaway container.

| Path | GB/s |
|---|---|
| raw RDMA, one rail / both rails | 14.0 / 23.1 |
| raw TCP, 8 streams across both rails | 10.5 |
| this protocol, receiver writing nothing | 6.0-6.2 |
| ssh (aes128-gcm), 1 stream / 8 streams | 1.21 / 4.74 |
| Qwen3-30B-A3B (57 GB), streaming | 2.5-3.0 |
| Qwen3-30B-A3B, end to end incl. verification | 2.79 (fresh disk) / 1.92 (after ~250 GB written that day) |

The copy is bound by the receiving NVMe. The peer's drive is 92% full, and its
direct-write rate fell from 5.96 GB/s to 1.86 GB/s over the session (fio, 16M
blocks, 4 jobs): the end-to-end figure is mostly fsync waiting for writeback.
Measured and rejected: round-robin chunk order across files (no faster, and
every file then ends at once, so the hash runs after the last byte) and
O_DIRECT writes (no faster, and verification then reads back from the same
saturated disk). 4, 8 and 16 streams land within noise of each other;
`DEFAULT_STREAMS` stays 8.

Then with NO addressing change at all (only the stale 10.100.0.2/30 here, the
rails found by link-local): Qwen3-4B 8.1 GB in 7.1 s, Qwen3-30B-A3B 61.1 GB in
34.8 s (1.76 GB/s end to end, disk-bound), both choosing `RoCE, 2 × 200 Gb/s`
with the 10 GbE LAN probed and left out.

**A copy over the LAN does not delay health checks, so it is not rate-capped.**
Measured 2026-11-21: Qwen3-4B (8.06 GB) sent to spark-4d38 over the 10 GbE
`enP7s7` (`path: network`, 2 streams) in 9.0 s end to end, 0.89 GB/s, while
`GET /agent/health` on that agent and `ping` were sampled every 250 ms:

| | health p50 / p95 / max | ping p50 / p95 / max |
|---|---|---|
| idle, 20 s | 1.41 / 2.13 / 4.34 ms | 0.33 / 0.85 / 0.87 ms |
| during the copy | 2.08 / 2.93 / 2.93 ms | 0.80 / 0.93 / 0.98 ms |

p95 rose 1.4x and no request took over 1 s, against a rule of "cap if p95 rises
more than 5x or any request takes over 2 s". The 2-stream cap
(`SLOW_LINK_STREAMS`) stays the only limit on a slow link.

**RDMA is not used for copies.** The streams are TCP over the RoCE ports. The
protocol carries 6 GB/s with the receiver writing nothing while the receiving
disk sustains ~2-3 GB/s, so RDMA verbs would move the bottleneck nowhere; it
would also add `pyverbs`/rdma-core to both the node image and the coordinator
(installed on neither) and registered-memory handling for every chunk.
