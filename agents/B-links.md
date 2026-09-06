# Agent B: Link Measurement

Read `00-architecture.md` first. Contracts there are frozen.

**You own:** `control_plane/links/**`, `tests/test_links.py`
**You depend on:** contracts, and Agent A's `RegistryPort` for node addresses
**Downstream of you:** E (planner). Your number is the input the entire product turns on.

---

## What you build

The component that measures what the interconnect actually delivers, rather than what the spec sheet claims.

This matters because the gap is large and consequential. GB10's ConnectX-7 negotiates 200GbE. Raw `ib_write_bw` shows roughly 24.6 GB/s. NCCL all-reduce delivers roughly 10.2 GB/s and sendrecv roughly 9 GB/s, because GPUDirect RDMA is disabled and GPU tensors route through system memory before reaching the NIC. Every framework in the ecosystem assumes the nameplate. That assumption is why NVIDIA's own playbook recommends a parallelism plan that loses to the alternative under batched load.

No existing tool does this step. It is the reason the product exists.

---

## The measurement

Run `all_reduce_perf` and `sendrecv_perf` from nccl-tests across the pair. Extract average bus bandwidth in GB/s and small-message latency in microseconds. Detect GPUDirect RDMA state, either from NCCL debug output mentioning GDR, or by checking whether the IB HCA reports it enabled.

Record all three plus the method into a `LinkMeasurement`. Both `all_reduce_gbps` and `sendrecv_gbps` are load-bearing and must be measured separately: all-reduce governs whether tensor parallel is viable, sendrecv governs pipeline stage handoff and KV transfer.

Measurement runs at cluster bring-up, on demand from `POST /api/links/measure`, and whenever a node is added. It does not run on a timer. It is disruptive and takes tens of seconds.

### When nccl-tests is unavailable

Fall back in this order:

1. `ib_write_bw` if present. Set `method="ib_write_bw"` and multiply the result by 0.42 to approximate NCCL-effective bandwidth, since that ratio is what the GDR-disabled path costs. Flag the estimate in the returned record.
2. A plain TCP throughput probe over the data-plane interface. Set `method="tcp"`.
3. Return `None` and let the planner run in conservative mode.

Never fabricate a measurement. A missing measurement is a state the planner handles; a wrong one produces a plan that silently underperforms.

---

## Storage

Persist to JSON on disk, keyed by unordered node pair. Survive restarts. Measurements older than 7 days report `stale=True`; the planner may still use them but the UI marks them.

`worst_all_reduce(node_ids)` returns the slowest pairwise measurement across a set, since the slowest link governs any collective over that set. Return `None` if any pair is unmeasured, rather than silently planning on partial data.

---

## Interface you must satisfy

```python
class LinkPort(Protocol):
    def get(self, a: str, b: str) -> LinkMeasurement | None: ...
    def worst_all_reduce(self, node_ids: list[str]) -> LinkMeasurement | None: ...
    def measure(self, a: str, b: str) -> LinkMeasurement: ...
```

Plus:

```python
def measure_all(self, node_ids: list[str]) -> list[LinkMeasurement]
def all(self) -> list[LinkMeasurement]
def put(self, m: LinkMeasurement) -> None      # manual override, method="manual"
```

---

## Day 0 stub

Return the fixture measurement: 10.2 GB/s all-reduce, 9.0 GB/s sendrecv, 40 microseconds latency, `gpudirect_rdma=False`, `method="nccl-tests"`. Agent E cannot start without this.

---

## Acceptance

- On two real Sparks, produces an all-reduce figure in the 8 to 12 GB/s range and correctly reports GDR disabled.
- Detects both QSFP ports and notes in the record if only one is lit, since a single port caps at roughly half.
- `worst_all_reduce` over three nodes returns the minimum, and returns `None` when any pair is unmeasured.
- Measurements survive a process restart.
- With nccl-tests absent, falls back and labels the method honestly.
- A measurement in progress does not block reads of existing ones.

## Traps

The two QSFP cages share PCIe Gen5 x4 links to the GB10, so a single cable caps near 100 Gb/s regardless of the 200GbE negotiation. Report which ports are active. Do not report raw RDMA as if it were NCCL bandwidth, that error is the whole reason the ecosystem's defaults are wrong. If a driver update enables GDR, the number moves and the planner's answer flips to tensor parallel; that is correct behavior and the reason nothing downstream may hardcode a constant.
