# registry

The cluster's picture of itself: which machines exist, what they are, whether they
are alive, and what they are doing right now. It is also the node agent — the one
process on every container, so a worker is a node agent and nothing else.

Two rules shape everything below. **Members and candidates are separate
collections**: discovery proposes, a human accepts, and a machine mDNS found sits
in `_candidates` so the UI can say "found on your network" without implying
anything will ever be scheduled onto it. **Health and telemetry never delete**: a
node that stops answering is marked unhealthy and keeps its last known numbers,
because greyed-out real values with a fault marker tell an operator more than a row
that vanished.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `registry.py` | 1297 | the coordinator's roster: join, admission, health, telemetry, reachability |
| `telemetry.py` | 660 | 1 Hz sampling, a 300-sample ring per node, and the two GB10 memory fallbacks |
| `agent.py` | 564 | the node agent app, present on every container in both roles |
| `startup.py` | 521 | the composition root the entrypoint calls; its order is load-bearing |
| `bootstrap.py` | 455 | role resolution — browse, join, else coordinate — and the re-announcement |
| `probe.py` | 441 | hardware to `NodeProfile`. Never raises |
| `shell.py` | 348 | an interactive pty on the host, over a WebSocket. A deliberate hole |
| `procs.py` | 323 | the narrow kill verb, bounded to PIDs holding GPU memory |
| `enrollment.py` | 281 | short-lived, spendable enrollment tokens — not the cluster token |
| `modelcache.py` | 262 | the downloaded weights, and getting rid of them |
| `storage.py` | 257 | filesystem capacity and what this product is spending it on |
| `discovery.py` | 248 | `_derate._tcp.local.`, advertised and browsed. zeroconf is a soft import |
| `reach.py` | 237 | can these two machines talk. Explicitly not the link measurement |
| `stub.py` | 206 | the day-0 `RegistryPort`, still reachable from two test modules |
| `serde.py` | 196 | explicit wire encoding, because these payloads cross a version boundary |
| `shell_config.py` | 175 | the shell's whole configuration, in one short file, off by default |
| `config.py` | 164 | `RegistryConfig.from_env`, and every operational timing constant |
| `identity.py` | 153 | the *cluster*'s id and permanent token, and the coordinator's banner |
| `shell_route.py` | 132 | `GET /agent/shell`, its own module because of an annotation trap |
| `net.py` | 126 | our address, and the refusal to start on a Docker bridge |
| `nodeident.py` | 118 | this *machine*'s id, seeded once and then kept |
| `hostfacts.py` | 98 | what the `/proc` readers fall through to on a machine with no `/proc` |
| `__init__.py` | 96 | 48 exported names, the largest public surface in the tree |
| `client.py` | 91 | the tiny HTTP interface the registry needs from other nodes |
| `roster.py` | 88 | the durable half of the roster, written atomically |
| `profiles.py` | 54 | one rule: an UNKNOWN profile never replaces an identified one |
| `labels.py` | 46 | a label is a name, never an identity |
| `errors.py` | 35 | four failure modes under a base class nothing raises |

## `registry.py`

`Registry` implements the frozen `RegistryPort` — `list_nodes`, `get_node`,
`healthy_nodes`, synchronous in-memory reads — and adds admission, health,
telemetry, labels and reachability. `add_node` and `handle_join` are async because
both probe the far side back before believing anything it said.

`handle_join` is the whole admission protocol and takes three credentials. A wrong
non-empty token is refused before any state is touched. An *absent* token routes the
caller into `offer_candidate` instead, which is the mechanism behind the zero-config
demo: two containers, the same command, no shared secret, and the second shows up
for a human to admit rather than founding a second cluster. An *enrollment* token
admits outright, because minting it was the admission decision. A tokenless caller
claiming to already be a member gets its status back and nothing else — node_ids are
slugified hostnames advertised in the clear over mDNS, so only a token holder may
move where a member's traffic is routed.

`enroll_local` is the one enrollment nobody clicks Admit for: there is no far side
to probe and no hop to authenticate. The constructor calls it with `persist=False`
and `start()` flushes it, without which a coordinator that had admitted nobody kept
a `registry.json` with no entry for the machine serving it.

Three loops run under `start()`. `health_round` and `telemetry_round` prime before
the first sleep, so the UI does not open on a row of zeros; `refresh_round` sleeps
first (`prime=False`), because `start_node` read the hardware moments ago and an
immediate re-probe would shell out to nvidia-smi to re-learn it. `apply_sample`
warns when `swap_used` rises: on unified memory an overcommitted pool does not
fail to allocate, it swaps, and decode throughput collapses.

`nodes_payload` is **not** what `GET /api/nodes` returns, whatever its docstring
said until it was corrected — that route builds rows with
`gateway.serialize.node_payload`, which carries eligibility, build skew and the
operator label this does not.

## `telemetry.py`

1 Hz sampling into a `RingBuffer` bounded by construction (`deque(maxlen=...)`,
`TELEMETRY_RING_SAMPLES` 300), so a generator running for days holds 300 samples
per node and not one more. Every nvidia-smi call is an async subprocess under
`TELEMETRY_TIMEOUT_S` (2.0) and the process is killed if it overruns, so a wedged
driver cannot stall the poll.

**GB10 needs two fallbacks and they fix different things.** A real DGX Spark
reports `[N/A]` for `memory.used`, `memory.total`, `memory.free` and
`memory.reserved` — unified memory, no discrete framebuffer to describe — so the
pool is read from `/proc/meminfo` instead, `MemAvailable` rather than `MemFree`.
Separately, `--query-compute-apps=used_gpu_memory` *does* return real numbers on
that part, and it is the only way to separate models from the operating system:
"a Spark with 68 GiB of model resident and 25 GiB of desktop has 27 GiB left, not
the 107 GiB that `usable_memory(0.90)` implies".

`allocatable_bytes` takes the smaller of the GPU's addressable headroom and what
the kernel says can be handed out without swapping, less `HOST_MEMORY_RESERVE`
(8 GiB). `allocatable_bytes_or_none` exists because the first answers an unsampled
node with its static ceiling, which is right for a display and wrong for a gate:
the caller cannot tell a measurement from a nameplate, and the fit gate goes on to
narrate it as "allocatable right now". `None` lets the live path drop the node so
the gate falls back to the ceiling *and says that is what it did*.

`read_gpu_processes` and `read_compute_apps` are three-valued on purpose: `None`
means nvidia-smi could not be asked, `[]` that the GPU is idle.

## `agent.py`

`NodeAgent` plus `create_agent_app`. FastAPI is imported *inside* the factory so
this module imports on a machine with no web framework — a convention with its own
cost, documented under `shell_route.py`.

The surface is reads — `/agent/profile`, `/agent/telemetry`, `/agent/health`,
`/agent/journal`, `/agent/processes`, `/agent/storage`, `/agent/models/cache` — plus
three routes gated on the cluster token, each for its own reason.
`POST /agent/processes/{pid}/kill` changes the machine, and what it may touch is
bounded in `procs.py` rather than at the route.
`DELETE /agent/models/cache/{folder}` deletes hundreds of gigabytes, its token
checked before the folder is looked at so an uncredentialled caller cannot learn
what is cached from which refusal comes back. `POST /agent/reach` makes *this*
machine dial an address the caller chooses, so uncredentialled it would be a port
scanner inside the operator's network. (The module docstring still calls the kill
"the only mutating route on this surface"; the cache delete arrived later.)

`profile_payload` carries `build`, deliberately not part of `NodeProfile` — the
profile is hardware and the software reading it is a different fact. A node whose
image predates a probe improvement reports hardware its coordinator would have
identified, and until this field existed the two were indistinguishable from
outside. `health_payload` carries it too, riding the 5 s heartbeat.

`reprobe_once` re-reads the hardware on the sample loop, two orders of magnitude
slower than telemetry, guarded by `profile_supersedes`. `set_token` exists so a
worker admitted late stops refusing credentialled requests for the life of the
process. `claim_shell` allows one live session per node — accountability, not a
resource limit: two prompts with no way to tell them apart is how somebody watches
a command they did not run and cannot find who did.

## `startup.py`

`start_node(config)` is the composition root, and the order is the module's whole
argument. Refuse a bridge network **first**, because every later step would appear
to work and then quietly discover nothing. Bootstrap the shell key next, so no
route exists before a key does. Load the persisted node id, probe the hardware,
recover credentials, then resolve the role — the profile is what we advertise and
what we join with.

`recover_credentials` reads back the permanent token a worker adopted earlier.
Nothing ever did, which made `adopt_cluster_token`'s write dead: a container
restarted with a spent `ej_` enrollment token still in its environment presented
the spent one and was 403'd out of a cluster it was already a member of, and a
native install restarted without the variable joined tokenless and then refused
every credentialled request. It uses `read_identity`, never
`load_or_create_identity` — a worker must not mint a cluster identity.

`NodeRuntime` holds what the container is running. `_announce_loop` runs in both
roles and only ever re-joins, so it adds no election. `_announce_trigger` fires on
two things and nothing else: our profile changed, or nobody has polled this node in
`REANNOUNCE_UNPOLLED_S`. It stands down while `_rejoin_loop` is waiting, and that
test is whether the loop is *running*, not what the status says — a rebuilt
coordinator re-offers a known node as a candidate, and gating on the status would
strand it. `_open_probe_sink` starts the TCP sink the link ladder's last rung dials;
`start_probe_server` was written, exported and documented as "the node agent starts
the sink", and then not called.

## `bootstrap.py`

Browse, join if somebody is already coordinating, otherwise coordinate. No
election and no failover: first one wins and the role is sticky for the process
lifetime. If the coordinator dies, workers keep running and the UI goes dark until
it comes back.

`resolve_role` never falls through to founding a cluster once a coordinator has
*answered*. A rejection proves one exists, so the decision is `ROLE_WORKER` with
`status="rejected"` and the rejoin loop keeps polling — including on the explicit
`DERATE_JOIN` path, where crashing looked like the loud failure but was not: on
first boot the join races this node's own agent app, so the coordinator's
probe-back lands before `/agent/*` is listening and even a perfectly configured
node 403s once.

`join_with_held_credentials` presents both credentials this machine may hold and
lets the far end settle it. `install.sh` bakes the enrollment token into the
container environment permanently, so after the first hour it is the wrong one —
but a *fresh* one carried here to re-home the machine is the right one and the
stored token is the wrong cluster's. Nothing on this side can tell those apart, so
the deliberate one is tried first. `rejoin_until_admitted` backs off
`REJOIN_INTERVAL_S` (15 s, jittered by 5 s) on "not yet" and
`REJOIN_WRONG_TOKEN_INTERVAL_S` (60 s) on a rejection — much more likely a foreign
cluster on the subnet than a typo about to fix itself.

## `probe.py`

`probe_local` turns a machine into a `NodeProfile` and **never raises**: an
unprobeable node is one the planner skips, not a crash, so every failure path
returns `unknown_profile` — zeroed, never partial.

The interesting work is what happens when nvidia-smi returns nothing, because three
different facts hide there. `_probe_apple` reads `machdep.cpu.brand_string` and
`hw.memsize`. `nvidia_hardware_present` reads `/proc/driver/nvidia/version` and PCI
vendor `0x10de` — both readable inside a container started *without* `--gpus`,
because the proc entry belongs to the loaded driver and the bus is the host's — and
a hit there stays UNKNOWN on purpose, because that shape is a misconfiguration and
the roster saying "unrecognised" is how anybody finds out. `_probe_cpu` requires one
host memory reading as evidence and returns `DeviceClass.CPU`: a Raspberry Pi is a
fact, not a gap, and UNKNOWN made the coordinator say it "cannot confirm this
hardware is eligible" about hardware it had identified.

On GB10 the memory numbers come from constants, not from nvidia-smi. That is
measurement, not caution: the part reports `[N/A]` for every FB memory field, and
the nameplate 128 GiB would also be wrong — 119.7 GiB is the GPU-reachable slice.
`MEMORY_BANDWIDTH_GBPS` is deliberately five entries long (H100 3350, A100 2039,
4090 1008, 3090 936, A6000 768); an unknown card returns 0.0 and is treated as
unmeasured, because a wrong bandwidth silently corrupts every plan reading it.

## `shell.py`

A remote-exec hole, opened deliberately. `procs.py` opens with the opposite intent
and every one of its three rules is bypassed here by construction: a shell is not
a bounded verb, it is every verb. The session lands on the **host** — `nsenter
--target 1` into PID 1's mount, uts, ipc, net and pid namespaces — so the blast
radius is the machine and, through the mounted SSH keys, the fleet.

What makes it defensible is that no gate is derived from a credential the network
can obtain. `DERATE_SHELL` unset means the route is never registered. `check_key`
fails closed with no key configured — an enabled shell with no key is
misconfigured, not open. `check_origin` is the shell's own Origin check — its
docstring still calls it the product's only one, and
`gateway/csrf.py::is_cross_site_write` has since become a second, refusing
cross-site writes under `/api` and `/v1`. It is still needed here, because that
middleware never sees this request: it is installed on the gateway app, the
handshake arrives on the agent's, and a WebSocket is exempt from CORS and never
preflighted anyway.

`Session.close` kills the **session**, not the process group, and that distinction
is the whole method. `pty.fork` calls `setsid`, but an interactive shell does job
control, which puts every background job in a group of its own — a `killpg` on the
leader reaches the shell and misses `sleep 300 &` entirely. So: hang up the
terminal, wait 2 s, then sweep by session id and SIGKILL what is left.
`open_session` builds argv, resolves the binary and materialises the environment
*before* the fork: the agent is multi-threaded, `forkpty` gives the child only the
calling thread, and a child that allocates before it execs can deadlock on a lock
nobody will ever release.

## `procs.py`

`kill_gpu_process(pid)` is the node agent's only capability that can touch a
workload sparkrun did not launch — a leftover `llama-server`, a driver orphaned by
a killed load run, a vLLM the coordinator lost across a restart. All of them hold
the pool the fit gate plans against and none are addressable by `sparkrun stop`.

Three rules keep it a verb. Only PIDs nvidia-smi *currently* reports as holding
GPU memory, re-read at kill time rather than taken from the caller, and checked
here rather than at the HTTP layer so no future route can skip it. Never PID 1,
never ourselves, never an ancestor (`guard`, `_ancestry`). And the caller must
hold the cluster token, enforced in `agent.py` where the headers are.

SIGTERM, `KILL_GRACE_S` (10 s), then SIGKILL and `KILL_FORCE_S` (5 s). **Success
means the memory came back, not that the signal was delivered**: `_settle` re-reads
the process list and reports what was actually reclaimed, because a process can
exit while the driver still holds its context. `_alive` treats a zombie as dead —
`os.kill(pid, 0)` succeeds against one, which would escalate to SIGKILL against a
corpse and report the memory as never reclaimed. `_require_posix_signals` refuses
the verb off POSIX by name: there `os.kill(pid, 0)` *terminates* what it is asked
to probe, every signal is `TerminateProcess`, and `signal.SIGKILL` does not exist.

## `enrollment.py`

The credential you are meant to copy. The cluster token in `identity.py` is
permanent, is printed once, and the documented way to bring up a second machine is
to carry it by hand — so the forever-secret ends up in a shell history, a chat
message and a screenshot, and it still only buys *candidate* status.

An enrollment token is minted on demand, expires (`DEFAULT_TTL_S` 3600, ceiling
`MAX_TTL_S` 86400), is spent after `DEFAULT_USES` (1), can be revoked, and admits
the joiner straight to member — minting it *is* the admission decision, made in
advance. `verify` compares against every live token with `hmac.compare_digest`
rather than looking one up by the id in its `ej_` prefix. `public()` keys on
`token_id`, never `token`.

It never replaces the cluster token: a node admitted this way is handed the
permanent one on the way in, so the enrollment token expiring cannot lock an
established member out. It is also not authentication for `/api`, which has none —
`POST /api/nodes/{node_id}/admit` is already open to anyone who can reach the port
— and must not be described as fixing that.

## `modelcache.py`

The other half of the disk question: `storage.py` says how full a filesystem is,
this says what is filling it. On the box it was written against the HuggingFace
cache held 894 GiB across 51 repositories, and `openai/gpt-oss-120b` alone was
182 GiB.

**Blobs are the bytes.** The cache stores every file once under `blobs/` and builds
`snapshots/<sha>/` out of symlinks into it, so walking snapshots counts the same
weights once per revision. `_blob_bytes` is one `scandir` of a flat directory —
6 ms for those 51 repositories.

**A repository is matched by folder name, never a decoded id.** `models--a--b--c`
could be `a/b--c` or `a--b/c`; `folder_for` encodes, which is exact, and
`repo_from_folder` decodes only for display. `resolve_target` bounds the delete
three ways — a single path segment, the `models--` prefix, and a resolved path whose
parent is the resolved cache — and lives here rather than at the route so no future
caller can reach `rmtree` without passing them. `delete` measures before and
confirms the directory's absence after, because "the call did not raise" and "the
bytes came back" are different claims.

## `storage.py`

Read on demand, never sampled. Disk moves hourly and a 1 Hz trace would cost the
durable journal two columns per node per second, so nothing here touches
`TelemetrySample` or the archive schema.

**Several paths are usually one filesystem.** The data root, the resolver cache and
the sparkrun cache are all under `/data` in the container. Reporting them as three
filesystems reports the same bytes three times — a 3.7 TB disk becomes 7.5 TB used
— so `read_filesystems` groups by `st_dev` and records every one of our paths in
`mount_paths`. Percentages are computed against `used + free`, not the raw device
size: a filesystem reserves blocks for root — 190 GiB on the 3.7 TB NVMe this was
written against — and `df` divides the same way, because a storage tab that
disagrees with `df` by four points is one an operator stops believing.

**A path we cannot read reports nothing, not zero.** `unreadable` carries the path
and a sentence; 0 bytes free reads as an emergency and 0 bytes used as an empty
disk. `DISK_WARN_PCT` (85) and `DISK_CRITICAL_PCT` (95) ship in the payload rather
than being left to the client, and are lower than the 90/95 used for memory because
memory is reclaimed when a process exits and a full disk leaves a half-downloaded
model behind.

## `discovery.py`

`Advertiser` registers `_derate._tcp.local.` with role, cluster_id and node_id in
TXT, so a browsing node can tell a coordinator from a worker before it tries to
join. It withdraws on `stop`: a stale advertisement outlives the process and points
the next node at a coordinator that is gone.

`readvertise` exists because the record is built once, in `start()`, from the
address this node had at boot — so a machine that took a new DHCP lease kept
advertising an address it no longer answered on, and `start()` returns early while
`_info` is set, so calling it again fixes nothing. It re-registers only if it was
registered: an advertiser that never started must not be started by a change of
address it was not announcing.

zeroconf is a soft import and `browse` returns `[]` when it is missing. The caller
cannot tell that from an empty network and does not need to — both mean "no
coordinator", and the answer to that is to become one. `browse_async` runs the
blocking 3 s browse (`MDNS_BROWSE_SECONDS`) on a thread.

## `reach.py`

The cheap question `links/measure.py` is not asking. That module saturates the
fabric for about a minute to find how *fast* a pair is; this asks whether a packet
gets there at all, from which side, and what the failure says — in `REACH_TIMEOUT_S`
(3.0 s), while a human waits, and before a bandwidth figure means anything.

Legs are directional and reported separately, because asymmetry is the interesting
case: a worker behind a NAT reaches the coordinator while the coordinator cannot
reach it back, and one merged "connected: false" hides the half that says what to
fix. `ms` is `None` whenever `ok` is false, never 0 — a zero beside "unreachable"
plots as a fast link. Three outcomes, not two: `summarize` separates a direction
that failed from one that could not be *checked* (`unknown_leg`), and only legs with
`pair=True` count toward "reachable" — reaching both machines from a third says
nothing about whether they can reach each other.

## `stub.py`

`StubRegistry` is the day-0 `RegistryPort`: two Sparks and a 3090, all healthy, with
a seeded 60-sample ring so `history()` answers on the first call. No probing, no
HTTP, no loops. Profiles come from `tests.fixtures` when importable so it cannot
drift from the frozen fixtures, and fall back to an inline copy inside the image.

It is not the gateway's stub — `gateway/stubs.py` has its own `StubRegistry`, and
that is what `contracts/routes.py` composes. This one is reached from
`tests/test_node_naming.py` and `tests/test_gateway_runtime.py`, and its docstring
still says "deleted at integration", which it was not.

## `serde.py`

Explicit wire encoding, kept ungeneric because these payloads cross a version
boundary between two containers that may not be the same build: an unexpected field
must be ignorable and a missing one must have a defined default.
`profile_from_dict` drops unknown keys and maps an unparseable `device_class` to
`UNKNOWN`.

Three of its functions exist because the same value was computed twice and
disagreed. `power_reading` returns `None` when `gpu_count == 0` — 0 W reads as a
measurement of an idle GPU rather than the absence of one — and it is shared rather
than inlined because the 1 Hz metrics frame and the topology payload each sent
`power_watts` straight through, so one node answered `null` on `/api/nodes` and
`0 W` on `/api/metrics/stream`, and the UI prefers the frame while it is fresh.
`temp_reading` is deliberately weaker: a GPU-less board usually does expose
`/sys/class/thermal`. `memory_used_pct` divides by `addressable_memory` and is
deliberately *not* `gateway.serialize.memory_used_pct`, which divides by physical
memory — on a GB10 those answer different questions.

## `shell_config.py`

The shell's entire configuration, in its own module rather than in `config.py`, so
a reader asking "can somebody get a root prompt on this box" finds the whole answer
in one short file. `enabled()` reads `DERATE_SHELL` and defaults to **off**, once at
startup rather than per request, so nothing arriving over the network can flip it
under a running process.

That default was briefly flipped to on, on the reasoning that the key is the real
gate and a present but unopenable door costs little. It was reverted: with the route
absent there is nothing to fingerprint and no key material loaded on a node nobody
intends to get a prompt on. Defence in depth survives one of its layers being
wrong; one gate does not.

`shell_key` reads `DERATE_SHELL_KEY` then the 0600 `shell.key` under the data root.
**No route mints it and no route returns it**, which is the point: `POST /api/enroll`
is unauthenticated and its token buys the permanent cluster token, so anything gated
on the *cluster* token is gated on a secret the LAN can mint for itself.
`bootstrap_key` prints a generated one with `print`, not `logging`, and only on the
boot that generated it — the log handler ships records to the coordinator and
archives them, and a secret in a queryable database is not out of band.

## `config.py`

`RegistryConfig.from_env` resolves `DERATE_ROLE`, `DERATE_TOKEN`, `DERATE_JOIN`,
`DERATE_AGENT_PORT` (8081), `DERATE_PORT` (8080), `DERATE_NODE_ID`,
`DERATE_CLUSTER_ID`, `DERATE_ALLOW_BRIDGE` and `DERATE_HOST_RESERVE_MIB` once. An
unrecognised role raises rather than defaulting. `data_dir` comes from
`control_plane.paths`, never a re-typed `/data`.

Every timing constant is read through `_const(name, default)`, so
`contracts/constants.py` wins wherever it defines one and the literal here is only
a fallback. Health is 5 s cadence, 2 s timeout, 3 misses — worst case an
unreachable node is unhealthy at ~12 s, inside the 15 s acceptance.
`REANNOUNCE_UNPOLLED_S` is one interval wider, so a node merely being marked down
does not start announcing itself in the same breath. `HOST_MEMORY_RESERVE` is
8 GiB — about 6% of a Spark's pool — because a model that fills a unified pool does
not fail to allocate, it pushes the OS into swap and takes inference down with it.

## `identity.py`

The *cluster*'s id and its permanent join token, generated on the coordinator's
first run and written at 0600. `os.open` creates the file at that mode *before*
anything is written, so the token is never briefly readable. An explicit
`DERATE_TOKEN` always wins and is persisted, so restarting with it set does not
silently keep an old one.

`read_identity` is the deliberate second entry point: `load_or_create_identity`
mints when nothing is on disk, which is right for a coordinator and wrong for a
worker, because a worker that minted an identity would be inventing a cluster
nobody asked for. It returns `None` for a missing, unreadable or tokenless file —
all "we have nothing", none worth failing over.

An unwritable data volume is a warning: the cluster still forms, the token just
does not survive a restart. `banner()` still prints the token — that is the
coordinator's own stdout on its own machine — but now tells the operator to mint a
one-hour enrollment token in the UI instead of carrying this one.

## `shell_route.py`

`GET /agent/shell`, in its own module, and that is not tidiness. `agent.py` imports
FastAPI *inside* `create_agent_app` so the module stays importable without a web
framework, and it carries `from __future__ import annotations` — so every
annotation is a string FastAPI resolves against the **module's** globals, and a
`WebSocket` imported into a function's local scope is not there. The route then
looks to FastAPI like an ordinary handler with a required query parameter called
`websocket`, and every handshake is refused with a validation error about a missing
field. **It fails as a 403, which reads exactly like the credential check working.**

So FastAPI is imported at the top here, and this module is imported only when the
shell is on. The key arrives in `Sec-WebSocket-Protocol` as the second entry after
`derate-shell`: a browser cannot set a header on a WebSocket handshake, and a query
parameter lands in access logs. Refusals close *before* `accept`, because Starlette
turns a close-before-accept into an HTTP status a caller can act on. Output is
binary and input JSON — terminal bytes are frequently not valid UTF-8 on their own,
so decoding would corrupt the stream, while input must carry a resize as well as
keystrokes.

## `net.py`

Two jobs. `primary_address` opens a UDP socket toward a public address to learn
which interface the routing table would pick; nothing is sent and nothing is
contacted. `require_host_networking` raises `BridgeNetworkError` with the fix in the
message, because mDNS does not cross a Docker bridge and a bridged container would
discover nothing, forever, silently.

`detect_bridge_networking` is evidence, not a name test: `in_container` checks
`/.dockerenv`, `/run/.containerenv` and `/proc/1/cgroup`, and the verdict is that
every non-loopback interface is in `_BRIDGE_ONLY_INTERFACES` — with `--network host`
the container would also see `docker0`, `enp*`, `wl*`. No interfaces at all returns
`False`; that is a different problem. `DERATE_ALLOW_BRIDGE` downgrades it to a
warning.

## `nodeident.py`

This *machine*'s id, seeded once from the hostname and then kept. Until this
existed it was re-derived every boot from
`slugify_hostname(socket.gethostname())`, so the hostname **was** the identity:
renaming a box, re-imaging it or letting DHCP hand it a new name turned it into a
stranger that arrived as a fresh candidate, while the machine it used to be stayed
in the roster forever as an unhealthy ghost — holding its label, its `links.json`
pairs, its `plan.node_ids` and its position on the cluster floor. Two machines
sharing a hostname collapsed onto one row.

This is not a re-keying: `node_id` is the same slug string it always was, and on
first boot after an upgrade the file is absent and the seed is exactly what the
previous code computed. Precedence is `DERATE_NODE_ID` > stored > seed, and an
override is *written*, the same rule `load_or_create_identity` applies to
`DERATE_TOKEN`.

## `hostfacts.py`

What the `/proc` readers in `telemetry.py` fall through to. On macOS and Windows
`/proc/meminfo`, `/proc/stat` and `/sys/class/thermal` all return `None` together,
`read_host_sample` returns `None`, and such a node joins, reports healthy and shows
nothing at all — the shape of failure this codebase most consistently refuses.

**It is reached by evidence, not identity.** Nothing here asks `sys.platform`; the
trigger is that `/proc/meminfo` could not be read, which is the fact the caller
actually needs. A name test would be wrong inside a Linux container on a Mac, under
WSL, and on a hardened `/proc` — all cases where the Linux readers still work and
should still win, so Linux never gets here and no existing reasoning changes.
psutil is a soft import: absent, this returns `None` and the machine reads exactly
as before. `temperature()` is always `None` on purpose — a motherboard zone
reported as a board temperature is a number nobody could tell from a measurement.

## `__init__.py`

48 names in `__all__`, the largest export surface in this tree (`contracts` is next
at 41), and the import path other packages use: `node.py` takes `NodeRuntime`,
`RegistryConfig` and `start_node`; `gateway/internal_api.py` takes `JoinRejected`
and `NodeNotFound`. Two consumers name a submodule instead —
`gateway/serialize.py` imports `power_reading` and `temp_reading` from
`registry.serde`, `contracts/routes.py` imports `NodeAgent` and `create_agent_app`
from `registry.agent` — which buys nothing at import time and should not be read as
a lighter path: every submodule here is imported eagerly at the top of this file,
so one name out of this package is `agent`, `registry`, `startup`, `telemetry` and
the rest, all of it.

## `client.py`

The `AgentClient` Protocol is what the registry needs from the outside world and
nothing more: `get_json` and `post_json`, both taking an explicit timeout. Health,
telemetry and probe-back can therefore be tested without a network or a mock HTTP
library.

`HttpAgentClient` builds its `httpx.AsyncClient` lazily with `max_connections=64`
and the same number of keepalives, because httpx's own default (100/20) is sized
for a general web client rather than a fleet of agents this coordinator controls.
Every failure becomes `ProbeFailed` carrying the method, URL and cause.

## `roster.py`

The durable half of the roster. `Registry.__init__` used to start every process
with empty `_members` and `_candidates`, so a coordinator restart forgot every
admitted worker. Profiles and agent URLs survive; live telemetry does not, because
it means nothing minutes later and the loops repopulate it within one round.

`save_roster` writes through tempfile + `fsync` + `os.replace`, so a crash mid-write
never leaves a half-written `registry.json` for the next start to trip over — this
module is deliberately more careful than `identity.py`, which persists the cluster
token with a plain truncating `os.open`. `load_roster` warns and starts empty on an
unparseable or non-object file; a *missing* one is silent, because that is every
first boot and a warning there would train an operator to ignore the ones that
mean something. A failed save is a warning, never an exception.

## `profiles.py`

One rule: **an UNKNOWN profile never replaces an identified one.** `UNKNOWN` is the
only device class that does not describe hardware — every other value is a positive
finding, and "could not look" is not evidence that anything changed.

The rule only became necessary when profiles started being written on a timer.
Before that the single writer was a join, which implies the far side just answered.
On a cadence that stops being true, because `probe_local` is total: its answer for
"I could not look" is a fully-formed `NodeProfile` saying UNKNOWN with everything
zeroed. One wedged `nvidia-smi` therefore flapped the roster GB10 → unknown → GB10,
flipping the node in and out of the serving pool while an operator watched a working
machine blink. It deliberately does not rank the identified classes against each
other — GB10 → CPU is believed, because that is what pulling a card looks like from
here. Only the absence of an answer is filtered.

## `labels.py`

An operator-chosen display name, and **a label is a name, never an identity**.
`node_id` stays exactly what it was — it is the key deployments, links, routing and
every plan on disk are written against — so this is a separate field the probe never
writes and the planner never reads.

`normalize_label` collapses whitespace runs (two spaces are invisible in the UI and
not in the JSON on disk), maps `None` and `""` to `None` so an operator who deletes
the name and saves gets the default back, and raises a sentence for control
characters or anything over `MAX_LABEL_LEN` (48) — long enough for "spark-4d38
(rack 2)", short enough for a chip-tier plate.

## `errors.py`

Five classes: `RegistryError`, a base nothing raises, and four failure modes an
operator can act on. `JoinRejected` deliberately carries no detail about the token,
so a wrong-token probe cannot be used as an oracle; the gateway maps it to 403.
`BridgeNetworkError` is thrown before any other startup work. `ProbeFailed` is for
*remote* HTTP probes only — the local hardware probe never raises, and the
docstring says so where somebody would assume symmetry.

## The seam with `node.py` and the gateway

`control_plane/node.py` is the only production caller of `start_node`. It brings
the node up, then — coordinator only — hands `runtime.registry` to `LinkService`,
`DeploymentManager` and `GatewayDeps`:

```python
runtime = await start_node(config)               # registry/startup.py
deps = build_gateway_deps(runtime, config)       # node.py; raises when
                                                 # runtime.registry is None
```

Everything else takes a narrow slice:

- **`gateway/internal_api.py`** takes `JoinRejected`, `NodeNotFound`,
  `serde.profile_from_dict`, and `modelcache` and `storage` directly for the
  coordinator's own disk routes.
- **`gateway/serialize.py`** takes `power_reading` and `temp_reading`, so the node
  row and the metrics frame cannot disagree about the same sample.
- **`gateway/livefit.py`** prefers `Registry.allocatable_or_none` over
  `available_memory` precisely because the first can say "nobody has measured this".
- **`gateway/capacity_api.py`** calls `Registry.memory_report`, the one view that
  separates the pool figure from the GPU figure — on GB10 that gap is the operating
  system, and an operator staring at a refusal needs to see it is the desktop in
  the way.
- **`gateway/shell_api.py`** takes `shell_config` alone, so the UI can say whether a
  prompt is available without loading the shell machinery.
- **`contracts/routes.py`** composes `create_agent_app(NodeAgent(...))` to enumerate
  the agent's routes without standing a node up.

## Things that look like details and are not

**On GB10 `nvidia-smi` reports aggregate memory as `[N/A]`, so only per-process
accounting works.** That is why the container runs `--pid=host`: inside its own PID
namespace nvidia-smi sees none of the processes holding the pool,
`read_compute_apps` returns 0, and every byte is attributed to the operating
system. It is also why `probe.py` takes the GB10 numbers from constants and
`telemetry.py` reads the pool from `/proc/meminfo`.

**The static ceiling must never be presented as headroom.** `NodeProfile.usable_memory`
is what the hardware could ever spend with nothing else running; on a part whose pool
is shared with an operating system it is the one figure guaranteed to be wrong. That
is what `allocatable_bytes_or_none` exists to keep sayable, and it is the spec-sheet
number this project exists to refuse to plan against.

**Platform questions are answered with evidence, not `platform.system()`.**
`nvidia_hardware_present` reads a proc entry and a PCI vendor id;
`detect_bridge_networking` reads the interfaces; `run_sysctl` is shaped like
`run_nvidia_smi` so a missing key and a missing binary are the same `None`; and
`hostfacts` is reached by an unreadable `/proc/meminfo`. The one deliberate
exception is `procs._require_posix_signals`, because there the platform genuinely
changes what `os.kill` *does*.

**Anything a runtime must keep between launches goes beside `hub/`, never inside
it.** `modelcache.scan` measures, and `modelcache.delete` removes, directories named
`models--*` directly inside the resolved hub directory. A compiled-kernel cache
placed under one of them would be counted as weights and deleted with them.

## Failure behaviour

- **Bridged container.** `require_host_networking` raises before any other work,
  with the `docker run --network host` line in the message.
- **zeroconf missing.** `browse` warns `ZEROCONF_MISSING` and returns `[]`,
  indistinguishable from an empty network, which is correct — both mean "become a
  coordinator". `Advertiser.start` returns `False`.
- **nvidia-smi absent, wedged or lying.** `probe_local` returns
  `unknown_profile(...)` with the reason logged; `profile_supersedes` stops it
  overwriting a good profile on the re-probe timer. `read_telemetry` returns `None`
  and the last sample is kept — host RAM where VRAM belongs is a wrong number, and
  worse than a stale one.
- **A node stops answering.** Unhealthy after 3 misses (~12 s), keeps its last
  telemetry, never removed. `_sample_node` returns early on `ProbeFailed`; the
  health loop owns that verdict.
- **The coordinator is gone.** Workers keep serving and keep journalling; the
  announce loop backs off from `REANNOUNCE_RETRY_S` (15 s) to
  `REANNOUNCE_MAX_RETRY_S` (120 s) and resets the moment somebody answers. No
  worker ever promotes itself.
- **A wrong token.** `JoinRejected` with no detail. `resolve_role` stays a worker
  because a coordinator demonstrably exists; `rejoin_until_admitted` retries at 60 s
  forever, and its only exit is a coordinator saying "member".
- **An unwritable data volume.** `roster.save_roster`, `identity.load_or_create_identity`,
  `nodeident.load_or_create_node_id`, `enrollment._save` and `shell_config.ensure_key`
  each warn and carry on; the state simply does not survive a restart, and each
  says so. A corrupt `registry.json` warns and starts empty.
- **Storage, processes or the model cache unreadable.** `available: false` with a
  sentence, never an empty list presented as an answer — "0 bytes free" for an
  unreachable node is how somebody concludes a disk is full when it is fine.
- **A kill that cannot be confirmed.** `_settle` reports reclaimed 0 and says
  nvidia-smi stopped answering. `gpu_unreadable` maps to 503,
  `not_a_gpu_process` to 404, `kill_not_permitted` to 403.
- **A shell with no key.** `check_key` refuses every session. `start_node`
  bootstraps one before the route exists, so `DERATE_SHELL=1` alone cannot produce
  a terminal that is advertised and turns everything away.

`tests/test_registry.py` (206 tests) is the bulk of the gate, with
`test_enrollment.py` (60), `test_gpu_processes.py` (29), `test_node_naming.py` (26),
`test_modelcache.py` (21), `test_storage.py` (20) and `test_shell.py` (17) alongside.

## Deliberately not built

**An election, and failover.** First one wins and the role is sticky for the process
lifetime. A worker whose coordinator has gone waits rather than promoting itself,
which is why the announce loop only ever re-joins and cannot split a subnet. The
coordinator is *replaceable* instead, via the cluster token — a different property,
and the one `identity.banner` tells the operator to preserve.

**A periodic re-announcement.** Two triggers only: our profile changed, or nobody
has polled us in `REANNOUNCE_UNPOLLED_S`. A timer on top of them would be constant
traffic to say nothing, so a healthy cluster runs the loop at zero network cost.

**A Windows port of the kill verb.** `_require_posix_signals` refuses by name: a
translation table is not enough, and until there is a real graceful-shutdown
mechanism, refusing beats a button reporting a clean drain it did not perform. The
same applies to `hostfacts.temperature`, which returns `None` permanently rather
than reporting a motherboard zone as a board temperature.

**In-use checks on the agent.** Neither `delete_cached_model` nor
`kill_gpu_process` asks whether a deployment is using what it is about to remove.
The agent has no idea what a deployment is; that check belongs to the coordinator.

**Fabric discovery.** `discovery.py` covers the management LAN only — ConnectX-7
subnets and the SSH mesh belong to sparkrun and NVIDIA Sync — and `reach.py` is
likewise not the link measurement.
