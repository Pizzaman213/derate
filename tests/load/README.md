# Load harness

Finds where the gateway breaks, and produces its derating curve.

```bash
python -m tests.load calibrate   # the harness's own ceiling, always run first
python -m tests.load rps         # offered request rate, 1 -> 200,000
python -m tests.load stream      # concurrent SSE streams, 1 -> 8192
python -m tests.load probes      # the cascades
python -m tests.load all --json result.json
pytest tests/test_load.py -m slow   # the SLO guards
```

## Layout

Three roles on separate cores, because a driver sharing a core with its subject
measures the wrong thing.

    core 0        the harness
    core 1        the gateway under test -- one process, one loop, as shipped
    cores 2-5     four fake vLLM runtimes
    cores 6-13    up to eight driver processes

The gateway is the real `create_app()` with real `GatewaySettings`. Only the
deployment port is swapped, for replicas pointing at the fake runtimes.
`GET /loadprobe` is the only addition: event-loop lag, CPU, RSS, per-target
outstanding, the admission KV ledger, and circuit states.

## Method

**Open loop.** Every request has a send time fixed before the run starts and is
timed from that intended moment, not from when a busy driver got to it. A
closed-loop driver self-throttles when the service slows and can never show
queue collapse, which is the thing being looked for.

**A/B every rung.** Each rung runs twice, once straight at the fake runtimes and
once through the gateway. Driver cost is in both, so the difference is the
gateway's. If the direct leg cannot hit the offered rate either, the rung is
reported `DRIVER-LIMITED` rather than as a finding.

**Calibrate first.** The driver's own ceiling is measured before the gateway is
touched: ~80,000 rps on this box, which is well clear of anything the gateway
reaches.

Keep-alive is mandatory throughout. With 28,231 ephemeral ports, unreused
connections exhaust the range above a few hundred requests a second.

## Reading the output

`err%` is answers that came back as failures; `drop%` is requests the driver
never sent because the gateway was too far behind to take them. They are
different failures and are reported separately. `cpu%` is of one core -- the
gateway is single-process, so 100% is the ceiling. `lag p99` is how long the
gateway's own event loop was blocked, which is the difference between "slower"
and "fell over".

A rung fails on: error rate above 1%, achieving under 90% of the offered rate,
p99 above 10x the idle p50, event-loop lag p99 above 250 ms, or any `outstanding`
counter still non-zero after the cooldown. The last passing rung is the derated
capacity.

## Measured, 2026-09-06

| Axis | Derated | Breaks at |
|---|---|---|
| Request rate | **500 rps** | 600 rps: goodput falls to 219 rps, p50 1.8 s, p99 17.9 s |
| Concurrent streams | **128** | 256: added inter-token p99 236 ms, loop lag p99 1,039 ms |

Both are congestive collapse, not graceful degradation: 20% more offered load
than capacity yields less than half the goodput.

### Probes: 5 held, 1 broke

**BROKE -- pool exhaustion misread as node death.** `PoolTimeout` from the
shared 256-connection pool is indistinguishable from an unreachable node in
`proxy.py`, so overload benches healthy targets: the breaker opened on **all
four** while every backend was serving normally. Half-open circuits then
persist, because `policies.py:50` breaks least-outstanding ties on `target_id`
and the lowest one wins every idle tie -- so the other replicas never receive
the single probe that would close them. At 10 and 50 rps, `d-1` served 100% of
traffic.

Held: abandoned streams release every commitment (audit H-1); the admission KV
ledger never leaked; the retry budget held amplification at **1.10x** across a
full 3,000-request 5xx storm; the 1 Hz index rebuild costs p50 2.5 ms / p99.9
5.7 ms (2x, bar 20x); control-plane traffic added +0.0 ms to inter-token
latency; killing every backend parked up to 64 requests with no RSS growth and
nothing leaked.

## Three harness bugs found by a peer review, and one found fixing them

The probe suite originally reported "every probe held" while two probes had in
fact never run. All four are fixed; they are recorded because each is a way a
verification tool can lie.

1. **The summary counted a probe that errored as a pass.** `broke=None` is
   falsy, so `any(...)` treated "did not run" as "held" and exited 0. A probe
   that did not run verified nothing. It now prints `DID NOT RUN` and exits 1.
2. **State pollution.** `pool_exhaustion` set `ttft_s=8.0` with no unconditional
   restore, so later probes measured conditions nobody established. Every probe
   now owns its full configuration through the `backend_config` context
   manager, which restores in a `finally`.
3. **`configure_backends` swallowed every exception.** A probe could not know
   its own preconditions held. It now verifies the echoed snapshot and raises
   `HarnessError`; `settle()` additionally requires one real request to return
   200, because idle counters are not readiness.
4. **The harness deadlocked its own subject.** `Proc` captured subprocess
   stderr with `stderr=subprocess.PIPE` and never drained it. The gateway logs
   a line per retried request, so once 64 KB filled the pipe buffer the next
   `write()` blocked **inside the gateway's event loop**: alive, 0% CPU,
   answering nothing, permanently. This presented exactly like a serious
   gateway bug -- a total 5xx storm wedging the process -- and was reproducible
   on a fresh cluster. It was the harness. Subprocess logs now go to files
   under `$DERATE_LOAD_LOGS`, and `error_storm` went from 873 answered plus 264
   unanswered to all 3,000 answered.
