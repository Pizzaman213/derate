"""A measured bf16 matmul rate, timed inside the serving image.

The first-token floor divides a prompt's matrix work by this number, so it has
to be the rate the runtime would actually get: the same torch, the same CUDA
and the same GPU as a launch. That is why it runs in the serving image (the
pattern of `links/collective.py`) rather than against whatever torch the host
happens to have, and why a failure is reported rather than replaced by a
datasheet figure -- a datasheet is not this machine.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

#: Runs inside the container. 8192^3 bf16 matmuls are large enough that launch
#: overhead vanishes and small enough to finish in seconds on any part.
_WORKER = r'''
import json, time
import torch
torch.cuda.set_device(0)
n = 8192
a = torch.randn(n, n, dtype=torch.bfloat16, device="cuda")
b = torch.randn(n, n, dtype=torch.bfloat16, device="cuda")
for _ in range(5):
    a @ b
torch.cuda.synchronize()
reps = 20
start = time.perf_counter()
for _ in range(reps):
    a @ b
torch.cuda.synchronize()
elapsed = time.perf_counter() - start
out = {
    "tflops": 2 * n ** 3 * reps / elapsed / 1e12,
    "device": torch.cuda.get_device_name(0),
    "torch": torch.__version__,
}
print("__derate_compute " + json.dumps(out), flush=True)
'''

_MARKER = "__derate_compute "


@dataclass(frozen=True)
class ComputeResult:
    tflops: float | None
    device: str | None
    error: str | None


def resolved_image() -> str:
    """The serving image, resolved exactly as the collective probe resolves it."""
    from control_plane.deploy.flags import runtime_spec

    return os.environ.get("DERATE_VLLM_IMAGE") or runtime_spec("vllm").default_image


def docker_argv(image: str) -> list[str]:
    return [
        "docker", "run", "--rm", "--gpus", "all", "--ipc=host",
        "--entrypoint", "python3", image, "-c", _WORKER,
    ]


def _parse(text: str) -> dict | None:
    for line in (text or "").splitlines():
        if line.startswith(_MARKER):
            try:
                found = json.loads(line[len(_MARKER):])
            except ValueError:
                return None
            return found if isinstance(found, dict) else None
    return None


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _failed(output: str) -> ComputeResult:
    tail = _last_line(output)
    message = "the matmul did not complete"
    return ComputeResult(None, None, f"{message}: {tail}" if tail else message)


def run_compute(*, image: str, timeout_s: float = 180.0) -> ComputeResult:
    """Time the matmul on this host's GPU 0. Never raises."""
    try:
        proc = subprocess.run(
            docker_argv(image),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return _failed(out or f"timed out after {timeout_s:.0f}s")
    except OSError as exc:
        return _failed(str(exc))
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    found = _parse(proc.stdout or "")
    if found is None:
        return _failed(output)
    try:
        tflops = float(found["tflops"])
    except (KeyError, TypeError, ValueError):
        return _failed(output)
    if tflops <= 0:
        return _failed(output)
    device = found.get("device")
    return ComputeResult(tflops, str(device) if device else None, None)
