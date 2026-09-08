# links

What the interconnect actually delivers, rather than what the spec sheet claims.
A GB10 pair negotiates 200GbE, shows roughly 24.6 GB/s to raw `ib_write_bw`, and
gives NCCL roughly 10 GB/s all-reduce, because GPUDirect RDMA is off and GPU
tensors transit system memory on their way to the NIC. Every framework in the
ecosystem plans against the nameplate, which is why NVIDIA's own playbook
recommends tensor parallel for two Sparks and loses to pipeline parallel under
batched load.

This is the number the rest of the product turns on, so nothing downstream may
hold it as a constant. If a driver update turns GDR on, the measurement moves and
the plan should flip. Read it from here, every time.

The rule that shapes every file below: **a measurement that is honest about being
a guess is worth more than a subtly-wrong figure.** When the ladder runs out of
rungs it returns `None`, and `None` is a state the planner handles.

## Layout

| File | Lines | What it owns |
|---|---|---|
| `measure.py` | 511 | the three-rung ladder — nccl-tests, `ib_write_bw` scaled and flagged, TCP |
| `parsers.py` | 258 | probe output to numbers, and `None` in preference to a guess |
| `service.py` | 256 | `LinkService` behind `LinkPort`: lock-free reads, serialized measurement |
| `record.py` | 178 | `AnnotatedLink` / `LinkAnnotation`, so the caveats ride with the number |
| `probe_server.py` | 175 | the built-in TCP sink and its client — the rung that needs no binary |
| `runner.py` | 164 | the fork-or-ssh seam every shell-out goes through |
| `qsfp.py` | 141 | which fabric cages are lit, and what one cable caps at |
| `stub.py` | 120 | the day-0 `LinkPort` fixture |
| `store.py` | 119 | durable JSON store; writes swap a whole mapping so readers never block |
| `gdr.py` | 103 | whether GPUDirect RDMA is actually on — the flag that explains the number |
| `__init__.py` | 57 | the 23-name export surface every other package imports through |

## `measure.py`

The ladder, tried in order, each rung labelled honestly for what it is.
`NcclMeasurer` runs both `all_reduce_perf` and `sendrecv_perf` under `mpirun` or
`srun`: all-reduce governs whether tensor parallel is viable, sendrecv governs
pipeline handoff and KV transfer, and neither is derivable from the other, so a
run that yields one and not the other is discarded and the ladder falls through.
`IbWriteBwMeasurer` scales raw RDMA by `IB_TO_NCCL_RATIO` and sets
`estimated=True`; it also records that `ib_write_bw` cannot tell the two
collectives apart, so both figures are the same estimate. `TcpMeasurer` is the
floor and `available()` returns `True` unconditionally, because the built-in sink
needs nothing installed.

**The probe sets nothing that would change the transport NCCL picks.**
`_run_test` exports `NCCL_DEBUG=INFO` and `NCCL_DEBUG_SUBSYS=INIT,NET` and
stops there — `NCCL_NET_GDR_LEVEL`, `NCCL_IB_DISABLE` and their relatives stay
untouched. The point is to measure what this cluster does, not what it could be
coaxed into doing, and `tests/unit/test_links.py::test_the_probe_does_not_tamper_with_the_transport_it_is_measuring`
asserts both names are absent from every argv the probe built.

**`_ClientSideMixin` runs the client half from endpoint a, not from here.**
`ib_write_bw` and `iperf3` are client/server, and the client's numbers describe
the path from wherever the client ran. Driving them from the coordinator would
measure coordinator-to-b and file the answer under a-to-b. So the client runs
locally when `a.is_local` and over ssh when it is not — and when it is not,
`TcpMeasurer._throughput` gives up on the built-in sink entirely rather than
substitute a link this node happens to be on.

`LadderMeasurer` records what it passed over on the way down: a record produced
by the second or third rung carries `tried first, without success: nccl-tests
(not installed)` as a note. A rung that raises is logged with `log.exception` and
counted as `(probe failed)`; it never takes the ladder down. `default_measurer()`
assembles the three in order. `DERATE_MPIRUN` names the launcher outright, and
otherwise `which` takes `mpirun` then `srun`; binary discovery reads
`DERATE_NCCL_TESTS_DIR` and `NCCL_TESTS_DIR` before falling back to `PATH` and
the four paths in `NCCL_TEST_DIRS`. `DERATE_MPIRUN_ARGS` is not discovery — it is
`shlex.split` in `NcclMeasurer.__init__` and appended to every launcher argv.

## `parsers.py`

Tolerant about column layout, strict about what it will claim. `parse_nccl_perf`
anchors on the numeric tail of a row rather than on fixed offsets, because op
variants differ in how many leading metadata columns they print, and
`_numeric_tail` counts `N/A` as part of the run since `#wrong` prints that way
when validation is off. Out-of-place and in-place measure the same operation, so
their mean is the estimate and neither is discarded.

**`bandwidth_gbps` averages only messages at or above `BW_FLOOR_BYTES` (16 MiB).**
nccl-tests sweeps upward from tiny messages; the small sizes are latency-bound and
report near-zero bus bandwidth, so averaging the whole sweep answers a different
question than "what does a collective get on this link". When a run contains no
large messages, `used_reported_average` goes true and the caller attaches a note
saying the tool's own whole-sweep average was used and understates throughput.

`parse_ib_write_bw` returns `(gbps, unit_seen)`, keeps the highest `BW average`
row of the `-a` sweep rather than averaging the sweep (the small sizes are
latency-bound), and reads the unit off the header rather than assuming it: perftest reports either Gb/sec or MB/sec, its MB is 2^20, and the GB/s
everything else here speaks is 10^9. `parse_ib_lat` reads `t_typical` or `t_avg`
off the same kind of header, and is the only collective-ish latency the
`ib_write_bw` rung ever gets before it drops to a TCP connect round trip.
`parse_iperf3` prefers `sum_received` over `sum_sent`, because the sender can
have bytes in flight that never landed, and falls through to
`_parse_iperf3_text` when the output is not JSON. Those three return `None` when
the figure is not there; `parse_nccl_perf` returns an `NcclResult` whose
`bandwidth_gbps` and `latency_us` are `None`, which is the same refusal one field
down. Nothing in this file invents a value: a wrong bandwidth number produces a plan
that silently underperforms and nobody ever finds out.

## `service.py`

`LinkService` is the component behind `LinkPort` — `get`, `worst_all_reduce`,
`measure`, plus `record`, `all`, `measure_all`, `put`, `forget` and `measuring`.
Reads are lock-free and always answerable, because `LinkStore` swaps whole
mappings; a thirty-second NCCL run never stalls the UI or the planner.
`_measure_lock` serializes probes, and `measuring()` exposes the in-flight pairs
so the screen can say a measurement is running.

**`worst_all_reduce` returns `None` if *any* pair in the set is unmeasured.**
The slowest link governs any collective over the set, so a partial picture would
silently reason over the fastest links we happen to know about and ignore the one
that will actually hurt. Fewer than two distinct nodes is also `None`: there is no
link to govern anything, and callers check node count before bandwidth.

**`_endpoint` prefers a data-plane address over the management IP.** In order:
the `data_plane_addresses` mapping, then `DERATE_DATAPLANE_<NODE_ID>` (uppercased
with non-alphanumerics replaced), then the registry's `address` or `hostname`,
then the node id dialled directly — node ids are hostnames in every deployment
shipped, and refusing to measure over a naming detail is worse than trying.
The management IP may not be on the ConnectX-7 fabric at all, and measuring the
management LAN answers the wrong question. `_is_local` decides which end runs the
client half, so it takes `local_node_id` or `DERATE_NODE_ID` first and only then
falls back to matching against `_local_names()`.

`put` stores a hand-entered figure with `method="manual"`, `estimated=True` and
a note saying to re-measure. The store lands at `data_dir() / DEFAULT_STORE_NAME`
(`links.json`), so it follows `paths.py` off `/data` when `/data` is not
writable. `DEFAULT_DATA_DIR` is kept only for callers that import it by name and
is `str(CONTAINER_DATA_DIR)` — the value comes from `control_plane.paths` as
well, so the alias cannot outlive the thing it aliases.

## `record.py`

`LinkMeasurement` is frozen in the contracts and gains no fields here.
`AnnotatedLink` subclasses it and adds one: a `LinkAnnotation` holding
`estimated`, `raw_gbps`, `scale_factor`, `active_ports`, `total_ports`,
`ports_inspected_on`, `gdr_detected_by`, `duration_s`, `stale` and free-text
`notes`. Downstream code that only knows the contract keeps working; code that
wants the caveats asks for them. `bare()` drops back to the exact contract type
for callers that serialize by field list.

**`IB_TO_NCCL_RATIO` is 0.42 and the record says out loud that it was applied.**
Reporting raw RDMA as if it were NCCL bandwidth is the exact error that makes the
ecosystem's defaults wrong; the scale factor is the correction and `raw_gbps`
keeps the pre-scaling number so the arithmetic is auditable. The ratio is applied
whether or not GDR is on, and where GDR is on the scaled figure may understate.

`pair_key(a, b)` sorts, because links are undirected and spark-01/spark-02 is one
link. `oriented()` re-presents a stored link from the caller's direction so nobody
has to notice which way round it was filed. `freshened()` recomputes `stale`
against the clock — staleness is a read-time fact, and `from_json` always sets it
`False` and lets the reader recompute. `STALE_AFTER_S` imports
`contracts.constants.LINK_STALE_SECONDS` when it exists; it does not today, so the
seven-day fallback is what runs. A stale measurement is still usable: the planner
may plan on it and the UI marks it, because refusing to plan on an old number is
worse than planning on one and saying so.

## `probe_server.py`

The bottom rung, and the reason there is one. The rungs above depend on tools
this project does not ship — nccl-tests, perftest, iperf3 — and a fallback that
needs an uninstalled binary is not a fallback. `start_probe_server()` binds a
threaded sink on `DEFAULT_PROBE_PORT` (47100); `tcp_throughput_gbps()` dials it,
sends the `SPLK0001` magic, blasts 1 MiB zero chunks for the duration, and reports
what the sink says it received. **The receiver's count is authoritative** —
bytes the sender handed to the kernel may still be sitting in a socket buffer
when the clock stops — and the sink starts its clock at the first byte rather
than at `accept()`, so connection setup does not count against throughput.

**`allow_reuse_address` is `os.name != "nt"`.** On POSIX `SO_REUSEADDR` rebinds a
port still in `TIME_WAIT` after a restart, which is wanted. On Windows it lets a
second process bind a port another process is actively listening on: both binds
succeed and which one answers a measurement is undefined. A link measurement that
silently reads the wrong socket is worse than one that does not happen, so on
Windows the second bind is allowed to fail and the caller reports the port taken.

`tcp_rtt_us()` takes the *minimum* of its samples, not the mean, because
scheduling noise only ever adds. What this file measures is wire throughput, never
labelled as NCCL bandwidth: the record it feeds carries `method="tcp"` and the
estimate flag.

## `runner.py`

Measurement is a two-machine operation and the two machines are reached
differently — one by fork, one by ssh. `CommandRunner` is the Protocol
(`run`, `run_on`, `which`, `read_text`, `glob`) and `SubprocessRunner` the real
implementation; every shell-out in this package goes through it, which is also
what makes the component testable without a pair of Sparks on the desk.

`CommandResult` carries `argv`, `returncode`, `stdout`, `stderr`, `duration_s`
and `timed_out`, with `ok` and `combined` derived. **A missing binary is an
ordinary outcome here, not an incident**: `OSError` and `ValueError` come back as
`returncode=127` rather than an exception, because the ladder's whole design is
to try things that may not be installed. A timeout returns `timed_out=True` with
whatever output was captured.

`DEFAULT_SSH_OPTS` sets `BatchMode=yes` so a probe never sits at a password
prompt, plus `StrictHostKeyChecking=no` and `ConnectTimeout=5`.
`BackgroundCommand` is the context manager for a listener on the peer — an
`ib_write_bw` or `iperf3` server — with a one-second settle before the client
dials, so a probe that fails partway through does not leave a listener bound.

## `qsfp.py`

The GB10's two QSFP cages each hang off a PCIe Gen5 x4 link, so **one cable tops
out near 100 Gb/s no matter what the port negotiated**. A pair cabled on a single
port therefore measures roughly half of a pair cabled on both, and the record has
to say which situation produced the number, or the next reader diagnoses a low
measurement as a driver problem.

`inspect_ports(runner, node_id)` reads `/sys/class/infiniband/*/ports/*` first and
falls back to `/sys/class/net/*/device/uevent` filtered on `_FABRIC_DRIVERS`
(`mlx5_core`, `mlx4_core`) for a ConnectX in Ethernet mode with no ib device
exposed. `PortStatus.note()` is the sentence an operator needs and returns nothing
when there is none. Active means the sysfs state reads `ACTIVE`, not `LinkUp`:
LinkUp alone means a cable is seated and the port is not carrying.

It is local by construction, and `PortStatus.inspected_on` names whose fabric was
read. `measure.py::_ports_for` will not call it at all when neither endpoint is
this node — attaching our own cages to a link between two other machines would be
a plausible-looking lie.

## `stub.py`

`StubLinkService` satisfies the whole `LinkPort` surface and returns real contract
types, because a stub that returns something else is worse than no stub at all.
Every pair answers with the fixture: `STUB_ALL_REDUCE_GBPS` 10.2,
`STUB_SENDRECV_GBPS` 9.0, `STUB_LATENCY_US` 40.0, `STUB_GPUDIRECT_RDMA` false and
`STUB_METHOD` `"nccl-tests"` — what two Sparks actually deliver with GDR off —
and a note saying it is a stub and should be replaced before anybody believes
it.

The `unmeasured` constructor argument lets a caller exercise the unmeasured path,
which is a real state the planner has to handle and the easiest one to forget;
`measure()` clears a pair out of it and `forget()` puts one back. The values are
duplicated rather than imported, because shipping code does not depend on the
test tree: they match `LINK_SPARK_10G` in `tests/fixtures/__init__.py` on the
five things `test_the_stub_returns_the_fixture_measurement` pins — both
bandwidths, the latency, the GDR flag and the method — and that test is what
keeps the copy honest. `measured_at` is deliberately not one of the five: the
fixture freezes it at 1757193600.0 and the stub stamps its own clock.

## `store.py`

A JSON-backed map from unordered node pair to `AnnotatedLink`, at
`SCHEMA_VERSION` 1. Two properties carry the file. Measurements survive a restart,
because re-measuring is disruptive and takes tens of seconds, and losing them on a
coordinator bounce would mean planning blind at exactly the wrong moment. And a
measurement in progress never blocks a read: `put` and `delete` build a new dict
and rebind `self._links` under `_write_lock`, so a reader touching that reference
can never observe a half-applied update and takes no lock at all.

`_flush` writes to a temp file beside the target, `fsync`s, and `os.replace`s, so
a crash mid-write leaves the previous good file rather than a truncated one. Links
serialize as a list, not an object, because node ids are opaque strings and this
package is not in the business of escaping them into JSON keys. An unreadable
store logs a warning and starts empty — everything in it is re-derivable by
measuring again, and the UI showing the pairs as unmeasured is the honest state.
An `OSError` on flush logs an error and leaves in-memory state standing: losing
durability is bad, losing the measurement just spent thirty seconds taking is
worse.

## `gdr.py`

**The flag that explains the number.** With GPUDirect RDMA off, a GPU tensor is
copied into system memory before the NIC ever sees it, which is why NCCL delivers
roughly 10 GB/s on a link that shows 24.6 GB/s to raw `ib_write_bw`. It is also
the flag most likely to change under us: a driver update that turns GDR on moves
the bandwidth and flips the planner's answer from pipeline to tensor parallel.
That is correct behaviour, and it is why nothing downstream may hold a constant
instead of reading this.

`detect_gdr(nccl_output, runner)` returns a `GdrEvidence` carrying `enabled`, a
`source` and a `detail`, so the claim is traceable in the record. NCCL's own
`GPU Direct RDMA (Enabled|Disabled)` line under `NCCL_DEBUG=INFO` wins, then a
`via NET/…GDRDMA` transport marker, then the negative inference: NCCL reported
`NET/IB` or `NET/Socket` and never mentioned GDR. Host inspection is the fallback
for when nccl-tests never ran, and it is deliberately weak — a loaded
`nvidia_peermem` or `nv_peer_mem` module is necessary and not sufficient, so
presence still reports `enabled=False` with the insufficiency spelled out. With no
evidence at all the answer is `source="unknown"`, disabled, "reported disabled
rather than assumed on".

## `__init__.py`

The import path, and the whole of it: 23 names in `__all__`. `LinkService` and
`StubLinkService`, `LinkStore`, the three rung classes — `NcclMeasurer`,
`IbWriteBwMeasurer`, `TcpMeasurer` — plus the `Measurer` protocol,
`LadderMeasurer` and `default_measurer`, `Endpoint`, `AnnotatedLink`,
`LinkAnnotation`, `annotate`, `pair_key`, `IB_TO_NCCL_RATIO`, `detect_gdr`,
`GdrEvidence`, `inspect_ports`, `PortInfo`, `PortStatus`, `ProbeServer`,
`start_probe_server` and `DEFAULT_PROBE_PORT`. Its docstring is the argument the
package exists to make, and is worth reading before the code.

## The seam with the node and the gateway

Nothing in `control_plane/` imports this package at module scope. `node.py`
builds the one real service inside `build_gateway_deps`, which imports lazily so
a worker process that never calls it never pulls the package in:

```python
from control_plane.links.service import LinkService

links = LinkService(registry=registry, local_node_id=runtime.profile.node_id)
```

`registry` is used only to turn a node id into an address; this package holds no
opinion about node health.

**`registry/startup.py::_open_probe_sink` starts the sink on every node**,
coordinator and worker alike — it runs before the coordinator/worker branch, and
wraps `start_probe_server()` in a `try`/`except OSError`. It was
written, exported, documented as "the node agent starts the sink", and then not
called — so the TCP rung only ever worked between two machines that both happened
to have iperf3 installed and reachable over ssh. It matters most on a machine with
no CUDA and no RDMA verbs, where TCP is the only rung it can take part in at all:
without a sink, every link to a Mac or a Windows box is permanently unmeasured
rather than coarsely measured. A port already in use is somebody else's sink or a
stale process, and neither is a reason for the node not to boot.

**`gateway/internal_api.py`** is the HTTP surface. `GET /api/links`, the `links`
of `GET /api/cluster` and the `edges` of `GET /api/topology` all go through
`_links_for`, which emits `measured: false` and *no numbers* for a pair that was
never probed.
`POST /api/links/measure` runs `LinkService.measure` on a thread and answers 503
— never a fabricated figure — when every rung failed. The planning path calls
`worst_all_reduce` after narrowing to the requested nodes, so
`plan.measured_link_gbps` never describes a link the plan does not cross.
`POST /api/links/reach` is deliberately a *different* route rather than a mode of
measure: measuring saturates the interconnect for about a minute, reachability
costs four health checks and is safe against a serving cluster.

**`gateway/serialize.py::link_payload`** reads the annotation through
`getattr(link, "annotation", None)` and omits every honesty field when there is
none. A bare `LinkMeasurement` — the stub, a hand-built fixture — must say
*nothing* about estimation rather than implying "measured, not estimated".

## Things that look like details and are not

**All-reduce and sendrecv are two measurements, not one.** All-reduce decides
whether tensor parallel is viable; sendrecv decides pipeline stage handoff and KV
transfer. On the fixture pair they are 10.23 and 8.985 GB/s — close enough to
tempt a single probe, far enough apart to change a plan. `NcclMeasurer` refuses to
report a run that produced only one of them, and `IbWriteBwMeasurer`, which
cannot tell them apart, sets both to the same estimate *and says so in a note*.

**`None` is a real answer and travels the whole way out.** `LinkPort.measure` is
typed `LinkMeasurement | None` as an adopted deviation from the frozen signature,
`LadderMeasurer` returns it after logging every rung it tried, `LinkService.get`
returns it for a pair never probed, and `POST /api/links/measure` turns it into a
503 saying every measurement method came back empty. Its body lists them as
`NCCL, RDMA probe, manual estimate`, which is not the ladder it just ran — the
third rung is TCP. The alternative to `None` is
inventing a number, and invented numbers are what put the rest of the ecosystem
wrong.

**The scale factor and the raw figure are both persisted.** `scale_factor` 0.42
and `raw_gbps` ride in the annotation and out through `link_payload`, so anybody
looking at an `ib_write_bw`-derived record can reconstruct the arithmetic instead
of trusting it. A record that carried only the scaled number would be
indistinguishable on screen from one NCCL actually produced.

**Port state is attached only when this node is an endpoint.** `_ports_for`
returns `source="not-observable"` when neither endpoint is local, and
`_port_notes` turns that into a note explaining that the cages we can see are not
the ones carrying this link. The alternative — reporting the coordinator's own
cages against a link between two other machines — reads as evidence and is not.

**Measurement is sequential and never on a timer.** `measure_all` walks pairs one
at a time; every probe saturates the fabric by design, so two at once produce two
wrong numbers instead of one right one. `_measure_lock` enforces it inside the
service. This is why measurement happens at bring-up and on demand only.

**The staleness window does not gate anything, and the flag on screen is not
this one.** `is_stale` is seven days and `freshened` recomputes it on every read,
but the planner may still use a stale measurement. What the UI renders is a
second copy of the window: `link_payload` never serializes `annotation.stale`,
and `internal_api._links_for` recomputes `stale` against its own
`STALE_LINK_AGE_S`, also seven days. Moving `STALE_AFTER_S` alone moves nothing
on screen. Refusing to plan on an old number is worse than planning on one and
saying so.

## Failure behaviour

- **nccl-tests absent, or half a run.** Fall to `ib_write_bw`, and the record
  that eventually wins carries `tried first, without success: nccl-tests …`.
- **A rung raises.** `log.exception`, counted as `(probe failed)`, ladder
  continues. One broken probe never takes the measurement down.
- **Every rung fails.** `LinkService.measure` returns `None`, `measure_all` logs
  a warning per pair, the route answers 503. No number is invented.
- **`ib_write_lat` missing.** Fall back to a TCP connect round trip and note on
  the record that it is a network RTT, not a collective latency. If endpoint a is
  not this node, return no latency at all — a round trip measured from here is
  the wrong round trip.
- **No large messages in the NCCL sweep.** Use the tool's own whole-sweep
  average and note that it understates a collective's throughput.
- **A parser cannot find its figure.** `None`, every time.
- **The store file is corrupt or unreadable.** Warn, start empty, show the pairs
  as unmeasured.
- **A single malformed record.** Skipped with a warning; the rest of the file
  loads.
- **The store cannot be written.** Log an error and keep the in-memory value.
- **The probe port is taken at startup.** Warn and run without a sink; links to
  this node fall back to whatever iperf3 is reachable, or stay unmeasured.
- **No address known for a node.** Dial the node id directly and log at debug.
- **The same node twice.** `measure` raises `ValueError("a link needs two
  distinct nodes")`; `get` on a self-pair is `None`.

`tests/unit/test_links.py` (930 lines, 66 tests) gates all of it, and is written
around the two failure modes rather than around coverage: a figure that is subtly
wrong, and a figure that is honest about being a guess. Its headline assertion is
`test_real_spark_pair_lands_in_the_acceptance_band_with_gdr_off` — 8 to 12 GB/s
all-reduce with `gpudirect_rdma` reported false.

## Deliberately not built

**No tuning of the transport being measured.** Setting `NCCL_NET_GDR_LEVEL` or
`NCCL_IB_DISABLE` would produce a number for a cluster that does not exist. The
probe exports debug variables only, and a test asserts it.

**No derived second collective.** sendrecv is not estimated from all-reduce even
though the ladder's own middle rung is forced to report them equal — and where it
is forced to, it prints the sentence saying so rather than letting the equality
pass as a result.

**No background re-measurement.** No timer, no scheduler, no refresh loop. A
probe saturates the fabric, so it happens at bring-up and when somebody asks.

**No shipped nccl-tests, perftest or iperf3.** All three are discovered, never
vendored, and `probe_server.py` exists precisely so the bottom of the ladder does
not depend on any of them.

**No inferred bandwidth for an unmeasured pair.** Not from a sibling link, not
from the port rate, not from the negotiated speed. `worst_all_reduce` would rather
return `None` for the whole set than answer from the pairs it happens to know.
