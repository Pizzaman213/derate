# sparkrun as derate's launcher: known limitations

sparkrun is scitrera.ai's launcher (Apache-2.0, alpha), version 0.2.40 on this
box, installed as a uv tool under `~/.local/share/uv/tools/sparkrun/`. It is
readable Python, not a binary. This note records what has gone wrong using it
as derate's launcher, the evidence, and the workaround or fix. Measured
2026-11-25 unless marked otherwise.

## 1. Downloads are slow and stall (xet path)

**Symptom.** Launching `unsloth/Qwen3.8-27B-GGUF:UD-IQ1_M` (6.73 GB) downloaded
at 4.5 MB/s, then stalled: NIC rx 0-2 MB/s, the `.incomplete` blob frozen.

**Evidence.** sparkrun's dist-info METADATA pins `huggingface-hub==1.8.0`
exactly, and its venv ships `hf_xet` 1.5.2 with no `hf_transfer`, so every
download goes through Hugging Face's xet storage path. From the same box a
plain HTTPS `curl` of the same file ran at 96 MB/s, and
`HF_HUB_DISABLE_XET=1 hf download unsloth/Qwen3.8-27B-GGUF Qwen3.8-27B-UD-IQ1_M.gguf mmproj-BF16.gguf`
fetched both files in 80 s; the sha256 matched the blob name. The
coordinator's own Python has hub 1.32 / hf_xet 1.6.0.

**Why derate cannot just set the variable.** `control_plane/deploy/sparkrun.py`
`sparkrun_env` (the `SPARKRUN_ENV_NAMES` allowlist) passes sparkrun
only an allowlist (`HF_HOME`, `HF_TOKEN`, `SPARKRUN_*`, `DERATE_*_IMAGE`, a
few system variables). `HF_HUB_DISABLE_XET` set on the coordinator never
reaches sparkrun.

**Workarounds.**
- Pre-download with `HF_HUB_DISABLE_XET=1 hf download ...` into the same hub
  cache. sparkrun's `snapshot_download` skips complete blobs, so the launch's
  own download becomes a no-op.
- The Models tab's Download (`control_plane/deploy/prefetch.py`) is NOT a way
  around it: its worker is started with the same `sparkrun_env()` allowlist
  (`prefetch.py:191`), so the variable is stripped there too.

**Caution.** A partial blob left by xet may not have been written front to
back. Delete the `.incomplete` file rather than resuming it over plain HTTP.

**Proposed fix (not done).** Allowlist `HF_HUB_DISABLE_XET` in `sparkrun_env`
(and default it to 1), which covers both sparkrun and the prefetch worker.
Upgrading sparkrun's hub violates its exact pin.
Bumping hf_xet with `uv tool install sparkrun --with 'hf_xet>=1.6' --reinstall`
is untested and reverts on the next sparkrun upgrade.

## 2. sparkrun's memory estimate is blind for GGUF

**Symptom.** The launch log prints "model_params not available" and
"KV cache estimate unavailable".

**Impact.** None. derate's fit gate sizes the launch and passes `--gpu-mem`
itself (e.g. 0.14 = 15.6 GiB; `control_plane/deploy/utilization.py`).

## 3. "Failed to pull updated image" for derate's images is expected

**Symptom.** `Failed to pull updated image: ghcr.io/pizzaman213/derate/vllm-audio:latest`.

**Explanation.** That image is unpublished and exists only as a local tag, so
the pull fails and sparkrun uses the local copy. The tts image is the same
story (CLAUDE.md, "This box"). Publishing the images is the real fix.

## 4. Stopping a launch mid-flight

- SIGTERM to the `sparkrun run` process while it is still downloading leaves
  no container; derate records `launching -> failed (sparkrun exited -15)`.
- Stopping a deployment while it is still launching tears it down and records
  `failed (stopped during launch)` (`DeploymentManager.stop`; seen for `d-7a25ee7d`).

## 5. Only sparkrun-named containers are adoptable

**Symptom.** A hand-run vLLM (`docker run`, e.g. `inkvllm`, published only on
loopback `127.0.0.1:18301`) is never adopted or routed; it shows only as
resident GPU memory: in the node inspector, and as a "not managed here" line on
its `/cluster` machine block.

**Evidence.** `control_plane/deploy/adopt.py:39` matches
`^(sparkrun_[0-9a-f]{6,})_[a-z0-9]+$`, and `control_plane/deploy/autoadopt.py`
probes the node address at the in-container port, which a loopback-only
publish never answers.

**Workaround.** Launch through derate (`POST /api/deployments` or the Serve
panel) so sparkrun names the container.

## 6. vLLM's EngineCore hides from command-line attribution

**Symptom.** vLLM renames its engine process's argv to `VLLM::EngineCore`, so
matching GPU processes to deployments by command line misses it.

**Fix (done).** `control_plane/gateway/gpu_procs.py` matches through container
lineage from `/agent/containers`: a cluster id may come from lineage, a port
only from the process's own command line, and live records are tried first.
Live-first matters because sparkrun's cluster id is deterministic (model,
hosts, port, name) and a stopped record's port is reused, so a relaunch shares
its old record's handle.

## 7. Already documented in CLAUDE.md (one line each)

- `--speculative-config` cannot go through `extra_args`; derate renders it
  itself (`deploy/flags.py::render_speculative_config`).
- In a solo launch a dead engine does not kill its container; `sparkrun logs`
  follows (`tail -f`) and `progress.py::_RUNTIME_FATAL` catches the failure.
- Cross-node expert parallel needs sparkrun >= 0.3.7 (`data_parallel_refusal`).
- sparkrun 0.3.x added a second hex segment to the cluster id
  (`sparkrun_<hex>_<hex>`); `CLUSTER_ID_RE` in `deploy/sparkrun.py:77` accepts both.
- The recipe has no `volumes:` key, so the HF cache is the container's only
  writable mount (`RuntimeSpec.cache_env`, `VLLM_CACHE_ROOT`).
- `gpus: ""` in the recipe's `executor_config:` must be quoted; a bare
  `gpus:` is null and falls through to `gpus: "all"`.
- sparkrun is Python source; read it under `~/.local/share/uv/tools/sparkrun/`
  when one of derate's literal regexes stops matching.

See CLAUDE.md, "Rules that are not style", for the full account of each.
