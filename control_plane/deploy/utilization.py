"""What fraction of a GPU to ask the runtime for.

`--gpu-memory-utilization` is not a limit. vLLM reads it as a claim on the
whole device -- it refuses to start unless ``fraction * total`` is **free**
right now, and then sizes its KV cache to fill that budget. So a constant
0.90 is a request for ninety percent of the machine, made identically for a
0.5B model and a 120B one.

That is what this module exists to stop. On the box it was written on, three
launches in a row died thirty seconds in:

    ValueError: Free memory on device cuda:0 (34.26/120.56 GiB) on startup is
    less than desired GPU memory utilization (0.9, 108.51 GiB).

One of them was `Qwen2-0.5B-Instruct-int4`. The fit gate had passed it
correctly, against the live budget, with its own arithmetic on the record:
**1.8 GiB predicted into 24.0 GiB usable, basis "live"**. Then the launch
asked the runtime for 108.5 GiB, because the flag had nothing to do with any
of that. The node was holding 84 GiB in a llama-server, a checkpoint server
and an unsloth session -- ordinary neighbours on a machine whose memory is
shared with the OS -- and every launch onto it failed no matter how small the
model was.

**The number the gate already computed is the number to ask for.** The fit
breakdown is weights + KV + activations + comm buffers + replicated +
framework overhead, for this model at this context and this concurrency. Ask
for that, and the runtime gets the budget the plan was approved against.
Asking for more does not make the model faster; it makes the KV cache bigger
than the plan called for, at the price of refusing to start on any machine
that is not empty.

Three bounds, in the order they bind:

*   **What the plan needs**, plus a small margin. Erring low is safe -- the
    runtime simply gets a smaller KV cache than budgeted -- and erring high
    is what fails a launch outright, so the margin is deliberately slight.
*   **What is free**, less a little headroom, because the check happens a
    moment after this is computed and a neighbour can allocate in between.
    Unknown free memory degrades to no cap at all, exactly like the fit
    gate's live-memory kwarg: never refuse for want of a live number.
*   **The guardrail**, which stays the ceiling it always was. This function
    can only ever ask for less than `DEFAULT_GUARDRAIL`, never more.
"""

from __future__ import annotations

from control_plane.contracts import DEFAULT_GUARDRAIL

#: Slack on top of the fit gate's own figure, for the difference between how
#: it accounts for a model and how a runtime does. Small on purpose: see the
#: first bound above -- low costs KV cache, high costs the whole launch.
REQUEST_MARGIN = 1.05

#: The share of free memory a launch may claim. The gap covers the moment
#: between reading this number and the runtime checking it, during which
#: anything else on a shared machine may allocate.
FREE_HEADROOM = 0.92

#: Never ask for less than this. A runtime handed a fraction that rounds to
#: nothing has no room for a KV cache and fails in a way that reads as a bug
#: in the model rather than a budget of zero.
#:
#: It is applied last, so on a machine with less free memory than this it wins
#: over the free-memory cap and the launch asks for more than is available.
#: That is deliberate: nothing can start on such a machine anyway, and the
#: refusal it produces -- "Free memory on device cuda:0 (2.1/120.6 GiB) on
#: startup is less than desired GPU memory utilization" -- names the actual
#: problem, where asking for the cap instead would fail later and less
#: legibly, somewhere inside the KV cache allocator.
MIN_UTILIZATION = 0.05


def utilization_for(
    *,
    needed_bytes: int,
    device_total_bytes: int,
    free_bytes: int | None = None,
    ceiling: float = DEFAULT_GUARDRAIL,
) -> float:
    """The `--gpu-memory-utilization` this launch should ask for.

    *needed_bytes* is the fit gate's own per-node total, *device_total_bytes*
    the denominator the runtime will use (the live sample, which is what CUDA
    reports and what the refusal above quotes), and *free_bytes* what the node
    can hand out now, or None when nothing sampled it.

    Degrades to *ceiling* when the device total is unknown, because a fraction
    computed against a denominator of zero is not a smaller request -- it is
    an arbitrary one.
    """
    if device_total_bytes <= 0 or needed_bytes <= 0:
        return ceiling

    wanted = (needed_bytes * REQUEST_MARGIN) / device_total_bytes

    cap = ceiling
    if free_bytes is not None and free_bytes > 0:
        cap = min(cap, (free_bytes * FREE_HEADROOM) / device_total_bytes)

    return max(MIN_UTILIZATION, min(wanted, cap))
