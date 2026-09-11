"""Planner-local tunables.

Anything the whole system shares lives in ``control_plane.contracts.constants``
and is imported, never re-declared. What is here is specific to how the planner
weighs one parallelism strategy against another.

Every value carries the measurement or constraint it came from. If you are
tempted to change one, change the comment first: if you cannot say what
measurement supports the new value, do not change the value.
"""

from __future__ import annotations

# Activations cross the wire in bf16 even when the weights are 4-bit. MXFP4 is a
# storage format for weights; the hidden states that TP all-reduces and PP hands
# off are 2 bytes per element.
ACTIVATION_DTYPE_BYTES = 2

# Two all-reduces per transformer layer under tensor parallel: one after the
# attention output projection, one after the MLP down projection. This is the
# number that makes TP expensive across a slow link -- on an 80-layer model it
# is 160 cross-node exchanges per output token against pipeline's one.
ALLREDUCES_PER_LAYER = 2

# Below this concurrency a latency target flips the answer to tensor parallel,
# because a 2-stage pipeline with no batch to fill it leaves half the stages
# idle. Above single stream the ordering reverses and pipeline wins.
#
# The mechanism is arithmetic and exact: at one in-flight request the bubble
# `(p-1)/p` cancels the `1/p` compute saving exactly, so a pipeline costs what
# ONE machine would cost at every degree -- see the note under
# `comm.pipeline_bubble_fraction`. Adding stages buys capacity, never speed.
#
# This comment used to cite "roughly 40 tok/s at single stream against
# pipeline's 29" as measured on GPT-OSS-120B across two Sparks. NO RECORD OF
# THAT MEASUREMENT EXISTS in `data_dir()/measurements/`, and the file header
# above says to change the comment first and not to change a value you cannot
# support. So it is demoted here to what it is -- an uncited claim -- rather
# than left reading as evidence. The value stands on the arithmetic, which
# does not need it.
LATENCY_CONCURRENCY_CEILING = 2

# Pipeline bubble guard. With fewer than this many in-flight requests per stage
# the bubble stops being amortised and tensor parallel may serve better.
PIPELINE_INFLIGHT_PER_STAGE = 4

# Cross-node expert parallel needs a cluster wide enough for expert sharding to
# buy something that tensor or pipeline parallel does not already buy. At EP=2
# the expert weights halve -- exactly what TP=2 and PP=2 also do -- while an
# all-to-all is added that neither of those needs. DeepEP's overlap only starts
# paying at real expert-dimension width. See NOTES.md for the spec conflict this
# resolves.
MIN_NODES_FOR_CROSS_NODE_EP = 4

# Context assumed when the caller does not name one. PlannerPort.plan() is
# frozen without a context parameter, but capacity depends on context, so the
# planner accepts it as an optional keyword and falls back to this.
DEFAULT_PLAN_CONTEXT = 32768

# Default KV cache element type when the caller does not name one. "auto"
# defers to the fit calculator's own per-model resolution (it follows the
# model's own dtype when that dtype is cacheable, bf16 otherwise) rather than
# the planner silently pinning a byte width of its own. Pinning a literal here
# used to be cosmetic: kv_dtype was accepted and stored but never forwarded to
# the capacity question, so this default -- and anything a caller passed --
# had no effect on min_nodes_required at all. Now that it is forwarded
# (``_facts``), "auto" keeps that historical no-op behavior for callers that
# do not care, while a caller that does name a dtype finally reaches the
# arithmetic it was meant to change.
DEFAULT_KV_DTYPE = "auto"

# Upper bound on the node count the capacity search will consider. A cluster
# larger than this is not a thing we plan for.
MAX_NODES_CONSIDERED = 64
