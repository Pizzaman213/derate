"""The `POST /api/deployments` body that reproduces a deployment.

One builder, so a crash relaunch, the UI's "Roll back to this", a boot
relaunch and a benchmark all send the same request. Absent options are
omitted, never sent as null: the launch route reads a missing key as "the
default", and a null is a different answer to some of its parsers.
"""

from __future__ import annotations

import shlex
from dataclasses import asdict, fields

from control_plane.contracts.deployment import Deployment, EngineOptions, Lifecycle

#: The `EngineOptions` fields whose wire form is a JSON object rather than
#: the contract's tuple of pairs.
_OBJECT_FIELDS = ("limit_mm_per_prompt", "generation_overrides", "env")


def engine_options_wire(options: EngineOptions) -> dict:
    """`options` in its request form, default-valued keys dropped."""
    out: dict = {}
    for f in fields(EngineOptions):
        value = getattr(options, f.name)
        if value == f.default:
            continue
        out[f.name] = dict(value) if f.name in _OBJECT_FIELDS else value
    return out


def launch_request_from(dep: Deployment) -> dict:
    """The launch body that reproduces *dep*."""
    plan = dep.plan
    body: dict = {
        "model_id": dep.shape.model_id,
        "served_name": dep.served_name,
        "runtime": dep.runtime,
        "context": dep.context_length,
        "concurrency": dep.max_concurrent_seqs,
        "node_ids": list(plan.node_ids),
        "parallelism": {
            "tensor_parallel": plan.tensor_parallel,
            "pipeline_parallel": plan.pipeline_parallel,
            "expert_parallel": plan.expert_parallel,
            "data_parallel": plan.data_parallel,
        },
    }
    if dep.kv_dtype:
        body["kv_dtype"] = dep.kv_dtype
    if dep.quantization:
        body["dtype"] = dep.quantization
    if dep.speculative is not None:
        spec: dict = {
            "method": dep.speculative.method.value,
            "num_speculative_tokens": dep.speculative.num_speculative_tokens,
        }
        if dep.speculative.model:
            spec["model"] = dep.speculative.model
        body["speculative"] = spec
    if dep.speculative_tokens > 0:
        body["speculative_tokens"] = dep.speculative_tokens
    if dep.extra_args:
        body["extra_args"] = " ".join(shlex.quote(t) for t in dep.extra_args)
    if dep.custom_command:
        body["custom_command"] = " ".join(shlex.quote(t) for t in dep.custom_command)
    if dep.enforce_eager:
        body["enforce_eager"] = True
    if dep.cudagraph_capture_sizes:
        body["cudagraph_capture_sizes"] = list(dep.cudagraph_capture_sizes)
    if dep.trust_remote_code:
        body["trust_remote_code"] = True
    options = engine_options_wire(dep.engine_options)
    if options:
        body["engine_options"] = options
    if dep.lifecycle != Lifecycle():
        body["lifecycle"] = asdict(dep.lifecycle)
    return body
