# Contracts, as the code has them

**Generated. Do not edit.** Regenerate with:

    python3 -m control_plane.contracts.manifest --write
    python3 -m control_plane.contracts.routes --write
    python3 -m control_plane.contracts.document --write

`tests/unit/test_contracts_manifest.py` fails when any of the three is stale, so
what follows is what the code said at the last commit that ran the suite.

This is the lookup surface. `00-architecture.md` is the design record and the
reasoning -- it is written as a journal, its appendices amend its early
sections, and reading it top-down will give you superseded answers. Look things
up here; read that for why.

## Enumerations
Every value that crosses the wire as a string.

| Type | Values | Defined in |
|---|---|---|
| `DeploymentState` | `planned`, `launching`, `ready`, `degraded`, `failed`, `stopping`, `stopped` | `control_plane.contracts.deployment` |
| `DeviceClass` | `gb10`, `discrete`, `apple`, `cpu`, `unknown` | `control_plane.contracts.hardware` |
| `Modality` | `text`, `embedding`, `speech`, `transcription` | `control_plane.contracts.modality` |
| `ParallelismKind` | `single_node`, `tensor`, `pipeline`, `expert`, `hybrid` | `control_plane.contracts.plan` |
| `ProviderKind` | `openrouter`, `openai`, `anthropic`, `together`, `groq`, `ollama`, `custom` | `control_plane.contracts.providers` |
| `RoutingPolicy` | `least_outstanding`, `round_robin`, `weighted_capacity`, `cache_affinity`, `failover`, `local_first`, `cost_aware` | `control_plane.contracts.routing` |
| `TargetKind` | `local`, `remote` | `control_plane.contracts.routing` |
| `Verdict` | `fits`, `fits_degraded`, `wont_fit` | `control_plane.contracts.plan` |

## Data shapes

### `Deployment`

`control_plane.contracts.deployment`

| Field | Type | Optional |
|---|---|---|
| `deployment_id` | `str` | no |
| `served_name` | `str` | no |
| `shape` | `ModelShape` | no |
| `plan` | `ParallelismPlan` | no |
| `fit` | `FitResult` | no |
| `runtime` | `str` | no |
| `state` | `DeploymentState` | no |
| `backend_url` | `str | None` | no |
| `context_length` | `int` | no |
| `max_concurrent_seqs` | `int` | no |
| `started_at` | `float | None` | no |
| `last_error` | `str | None` | no |
| `modality` | `Modality` | yes |
| `extra_args` | `tuple[str, ...]` | yes |
| `custom_command` | `tuple[str, ...]` | yes |

### `FitRequest`

`control_plane.contracts.plan`

| Field | Type | Optional |
|---|---|---|
| `shape` | `ModelShape` | no |
| `context_length` | `int` | no |
| `max_concurrent_seqs` | `int` | no |
| `kv_dtype` | `str` | no |
| `plan` | `ParallelismPlan` | no |
| `weight_bytes` | `int | None` | yes |
| `native_window` | `int | None` | yes |

### `FitResult`

`control_plane.contracts.plan`

| Field | Type | Optional |
|---|---|---|
| `verdict` | `Verdict` | no |
| `breakdown` | `MemoryBreakdown` | no |
| `usable_per_node` | `int` | no |
| `headroom` | `int` | no |
| `reason` | `str` | no |
| `limiting_term` | `str` | no |
| `max_context_that_fits` | `int | None` | no |
| `predicted_decode_tps` | `float | None` | no |
| `warnings` | `list[str]` | yes |
| `budget_basis` | `str` | yes |

### `GpuProcess`

`control_plane.contracts.hardware`

| Field | Type | Optional |
|---|---|---|
| `pid` | `int` | no |
| `name` | `str` | no |
| `command` | `str | None` | no |
| `user` | `str | None` | no |
| `gpu_memory` | `int` | no |

### `LinkMeasurement`

`control_plane.contracts.hardware`

| Field | Type | Optional |
|---|---|---|
| `src` | `str` | no |
| `dst` | `str` | no |
| `all_reduce_gbps` | `float` | no |
| `sendrecv_gbps` | `float` | no |
| `latency_us` | `float` | no |
| `gpudirect_rdma` | `bool` | no |
| `measured_at` | `float` | no |
| `method` | `str` | no |

### `MemoryBreakdown`

`control_plane.contracts.plan`

| Field | Type | Optional |
|---|---|---|
| `weights` | `int` | no |
| `kv_cache` | `int` | no |
| `activations` | `int` | no |
| `comm_buffers` | `int` | no |
| `replicated` | `int` | no |
| `framework_overhead` | `int` | no |

### `ModelShape`

`control_plane.contracts.model`

| Field | Type | Optional |
|---|---|---|
| `model_id` | `str` | no |
| `num_layers` | `int` | no |
| `hidden_size` | `int` | no |
| `num_attention_heads` | `int` | no |
| `num_kv_heads` | `int` | no |
| `vocab_size` | `int` | no |
| `total_params` | `int` | no |
| `dtype` | `str` | no |
| `head_dim` | `int | None` | yes |
| `num_experts` | `int` | yes |
| `num_experts_per_token` | `int` | yes |
| `active_params` | `int | None` | yes |
| `sliding_window` | `int | None` | yes |
| `layers_with_full_attention` | `int | None` | yes |
| `mla_latent_dim` | `int | None` | yes |
| `mla_rope_dim` | `int | None` | yes |
| `vision_params` | `int` | yes |
| `is_encoder_decoder` | `bool` | yes |

### `NodeProfile`

`control_plane.contracts.hardware`

| Field | Type | Optional |
|---|---|---|
| `node_id` | `str` | no |
| `hostname` | `str` | no |
| `address` | `str` | no |
| `device_class` | `DeviceClass` | no |
| `gpu_name` | `str` | no |
| `gpu_count` | `int` | no |
| `total_memory` | `int` | no |
| `addressable_memory` | `int` | no |
| `memory_bandwidth_gbps` | `float` | no |
| `compute_capability` | `str` | no |
| `driver_version` | `str` | no |

### `NodeState`

`control_plane.contracts.hardware`

| Field | Type | Optional |
|---|---|---|
| `profile` | `NodeProfile` | no |
| `healthy` | `bool` | no |
| `last_seen` | `float` | no |
| `memory_used` | `int` | no |
| `power_watts` | `float` | no |
| `temperature_c` | `float` | no |
| `utilization_pct` | `float` | no |
| `memory_total` | `int` | yes |
| `sample_ts` | `float` | yes |
| `is_local` | `bool` | yes |
| `build` | `str` | yes |

### `ParallelismPlan`

`control_plane.contracts.plan`

| Field | Type | Optional |
|---|---|---|
| `kind` | `ParallelismKind` | no |
| `tensor_parallel` | `int` | no |
| `pipeline_parallel` | `int` | no |
| `expert_parallel` | `int` | no |
| `data_parallel` | `int` | no |
| `node_ids` | `list[str]` | no |
| `reason` | `str` | no |
| `measured_link_gbps` | `float` | no |
| `rejected` | `list[str]` | no |

### `Provider`

`control_plane.contracts.providers`

| Field | Type | Optional |
|---|---|---|
| `provider_id` | `str` | no |
| `kind` | `ProviderKind` | no |
| `display_name` | `str` | no |
| `base_url` | `str` | no |
| `api_key_ref` | `str` | no |
| `enabled` | `bool` | no |
| `priority` | `int` | no |
| `models` | `list[ProviderModel]` | yes |
| `healthy` | `bool` | yes |
| `last_error` | `str | None` | yes |
| `last_refreshed` | `float` | yes |

### `ProviderModel`

`control_plane.contracts.providers`

| Field | Type | Optional |
|---|---|---|
| `served_name` | `str` | no |
| `upstream_id` | `str` | no |
| `context_length` | `int` | no |
| `supports_streaming` | `bool` | no |
| `supports_tools` | `bool` | no |
| `input_cost_per_mtok` | `float | None` | no |
| `output_cost_per_mtok` | `float | None` | no |
| `modality` | `Modality` | yes |

### `QuantInfo`

`control_plane.contracts.quant`

| Field | Type | Optional |
|---|---|---|
| `key` | `str` | no |
| `bits_per_weight` | `float` | no |
| `family` | `str` | no |
| `native_compute_capability` | `float | None` | no |
| `emulated_below_native` | `bool` | no |
| `note` | `str` | yes |

### `RouteTarget`

`control_plane.contracts.routing`

| Field | Type | Optional |
|---|---|---|
| `target_id` | `str` | no |
| `kind` | `TargetKind` | no |
| `backend_url` | `str` | no |
| `weight` | `float` | no |
| `outstanding` | `int` | no |
| `healthy` | `bool` | no |
| `admitting` | `bool` | no |
| `strength` | `float` | no |
| `cost_per_mtok` | `float | None` | no |

### `RoutingConfig`

`control_plane.contracts.routing`

| Field | Type | Optional |
|---|---|---|
| `served_name` | `str` | no |
| `policy` | `RoutingPolicy` | no |
| `targets` | `list[RouteTarget]` | yes |
| `sticky_ttl_s` | `int` | yes |

## Ports

The interfaces components hold each other to.

| Port | Methods |
|---|---|
| `DeploymentPort` | `get`, `launch`, `list`, `stop` |
| `FitPort` | `check`, `max_context` |
| `LinkPort` | `get`, `measure`, `worst_all_reduce` |
| `PlannerPort` | `plan` |
| `ProviderPort` | `add`, `health`, `list`, `models`, `refresh`, `resolve_key` |
| `RegistryPort` | `get_node`, `healthy_nodes`, `list_nodes` |
| `ResolverPort` | `resolve` |

## Constants

| Name | Value | Defined in |
|---|---|---|
| `BYTES_PER_PARAM` | (33 entries) | `control_plane.contracts.quant` |
| `COMM_BUFFER_BYTES` | `1610612736` | `control_plane.contracts.constants` |
| `DEFAULT_DTYPE` | `bf16` | `control_plane.contracts.quant` |
| `DEFAULT_GUARDRAIL` | `0.9` | `control_plane.contracts.hardware` |
| `DEGRADED_TPS_THRESHOLD` | `10.0` | `control_plane.contracts.constants` |
| `ENDPOINT_FOR_MODALITY` | (4 entries) | `control_plane.contracts.modality` |
| `EP_EXTRA_BUFFER_BYTES` | `2147483648` | `control_plane.contracts.constants` |
| `EP_VIABLE_THRESHOLD` | `40.0` | `control_plane.contracts.constants` |
| `FRAMEWORK_OVERHEAD` | `1073741824` | `control_plane.contracts.constants` |
| `GB10_ADDRESSABLE` | `128526896332` | `control_plane.contracts.constants` |
| `GB10_MEM_BANDWIDTH` | `273.0` | `control_plane.contracts.constants` |
| `GB10_TOTAL_MEMORY` | `137438953472` | `control_plane.contracts.constants` |
| `QUANT_INFO` | (33 entries) | `control_plane.contracts.quant` |
| `TP_VIABLE_THRESHOLD` | `40.0` | `control_plane.contracts.constants` |

## Derived facts

Facts about the shapes above that live outside `contracts/`, and every
site that restates one. `tests/unit/test_single_source.py` holds the copies to
the canonical value.

### `binary_byte_formatter`

- **Canonical**: `control_plane.humanize:binary_bytes` = `<function control_plane.humanize.binary_bytes>`
- **Why not in `contracts/`**: a rendering concern, not a contract, and the fit calculator that wrote it needs it in the same sentence as a refusal. Kept out of planner/comm.py on purpose: that one formats transfer volumes beside decimal GB/s bandwidths and is right to stay decimal
- **Checked copies**: `control_plane.fit.calculator:_gib`

### `default_agent_port`

- **Canonical**: `control_plane.registry.config:DEFAULT_AGENT_PORT` = `8081`
- **Why not in `contracts/`**: the node agent's own default, read by everything that dials one
- **Restated (not checkable)**: `control_plane/gateway/settings.py`, `control_plane/gateway/enroll_api.py`, `control_plane/registry/agent.py`, `install.sh`, `compose.yaml`

### `default_coordinator_port`

- **Canonical**: `control_plane.registry.config:DEFAULT_COORDINATOR_PORT` = `8080`
- **Why not in `contracts/`**: the coordinator's own default, same reason
- **Restated (not checkable)**: `control_plane/gateway/main.py`, `control_plane/deploy/sparkrun.py`, `install.sh`, `compose.yaml`

### `deployment_serving_states`

- **Canonical**: `control_plane.deploy.fsm:SERVING` = `degraded`, `ready`
- **Why not in `contracts/`**: same as the terminal set: derived from the enum, enforced by the fsm
- **Checked copies**: `control_plane.gateway.states:SERVING`

### `deployment_terminal_states`

- **Canonical**: `control_plane.deploy.fsm:TERMINAL` = `failed`, `stopped`
- **Why not in `contracts/`**: a judgement about DeploymentState rather than part of it, and it belongs beside the transition table that enforces it. The gateway cannot import it -- the deploy package __init__ pulls the manager, the sparkrun adapter and the event bus into a request module -- so gateway/states.py holds the gateway's one spelling and this holds the two together
- **Checked copies**: `control_plane.gateway.states:TERMINAL`

### `node_roles`

- **Canonical**: `control_plane.registry.config:ROLE_COORDINATOR` = `coordinator`
- **Why not in `contracts/`**: the role a node takes is the registry's question to answer

### `redacted_placeholder`

- **Canonical**: `control_plane.redaction:REDACTED` = `***`
- **Why not in `contracts/`**: it belongs with the scrubber that writes it, and that scrubber is no longer inside the provider package -- logfiles.py redacts on every node, and the worker path may not import providers
- **Checked copies**: `control_plane.providers.config:REDACTED`, `control_plane.gateway.serialize:REDACTED`

### `runtime_names`

- **Canonical**: `control_plane.deploy.flags:SUPPORTED_RUNTIMES` = `vllm`, `sglang`, `tts`
- **Why not in `contracts/`**: the launcher owns which runtimes exist, because a runtime without a RuntimeSpec cannot be launched whatever else claims to know it
- **Checked copies**: `control_plane.resolver.support:RUNTIMES`

## HTTP surface

### Coordinator gateway (78 routes)

| Method | Path |
|---|---|
| GET | `/api/activity` |
| GET | `/api/capacity` |
| GET | `/api/catalog` |
| GET | `/api/cluster` |
| GET | `/api/deployments` |
| POST | `/api/deployments` |
| DELETE | `/api/deployments/{deployment_id}` |
| GET | `/api/deployments/{deployment_id}` |
| GET | `/api/deployments/{deployment_id}/logs` |
| GET | `/api/docs` |
| GET | `/api/enroll` |
| POST | `/api/enroll` |
| DELETE | `/api/enroll/{token_id}` |
| GET | `/api/history/events` |
| GET | `/api/history/logs` |
| GET | `/api/history/nodes` |
| GET | `/api/history/requests` |
| GET | `/api/history/status` |
| GET | `/api/links` |
| POST | `/api/links/measure` |
| POST | `/api/links/reach` |
| GET | `/api/memory` |
| GET | `/api/metrics/stream` |
| GET | `/api/models` |
| GET | `/api/models/detail` |
| GET | `/api/models/quant-table` |
| GET | `/api/models/search` |
| GET | `/api/models/variants` |
| GET | `/api/nodes` |
| GET | `/api/nodes/candidates` |
| POST | `/api/nodes/join` |
| DELETE | `/api/nodes/{node_id}` |
| GET | `/api/nodes/{node_id}` |
| POST | `/api/nodes/{node_id}/admit` |
| PUT | `/api/nodes/{node_id}/label` |
| GET | `/api/nodes/{node_id}/memory` |
| GET | `/api/nodes/{node_id}/processes` |
| DELETE | `/api/nodes/{node_id}/processes/{pid}` |
| GET | `/api/nodes/{node_id}/runtime` |
| POST | `/api/nodes/{node_id}/runtime` |
| POST | `/api/nodes/{node_id}/runtime/model` |
| GET | `/api/openapi.json` |
| POST | `/api/plan` |
| GET | `/api/providers` |
| POST | `/api/providers` |
| GET | `/api/providers/kinds` |
| GET | `/api/providers/secret-refs` |
| DELETE | `/api/providers/{provider_id}` |
| PATCH | `/api/providers/{provider_id}` |
| GET | `/api/providers/{provider_id}/backends` |
| GET | `/api/providers/{provider_id}/logo` |
| GET | `/api/providers/{provider_id}/models` |
| POST | `/api/providers/{provider_id}/pull` |
| POST | `/api/providers/{provider_id}/refresh` |
| GET | `/api/publishers/avatars` |
| GET | `/api/publishers/{owner}/avatar` |
| GET | `/api/routing` |
| DELETE | `/api/routing/{served_name}` |
| PUT | `/api/routing/{served_name}` |
| GET | `/api/settings` |
| PATCH | `/api/settings` |
| GET | `/api/setup` |
| POST | `/api/setup/complete` |
| GET | `/api/shell/status` |
| GET | `/api/storage` |
| DELETE | `/api/storage/cache/resolver` |
| DELETE | `/api/storage/nodes/{node_id}/models/{folder}` |
| GET | `/api/topology` |
| GET | `/docs/oauth2-redirect` |
| GET | `/healthz` |
| GET | `/install.sh` |
| POST | `/v1/audio/speech` |
| POST | `/v1/audio/transcriptions` |
| GET | `/v1/audio/voices` |
| POST | `/v1/chat/completions` |
| POST | `/v1/completions` |
| POST | `/v1/embeddings` |
| GET | `/v1/models` |

### Node agent (11 routes)

| Method | Path |
|---|---|
| GET | `/agent/health` |
| GET | `/agent/journal` |
| GET | `/agent/models/cache` |
| DELETE | `/agent/models/cache/{folder}` |
| GET | `/agent/processes` |
| POST | `/agent/processes/{pid}/kill` |
| GET | `/agent/profile` |
| POST | `/agent/reach` |
| GET | `/agent/storage` |
| GET | `/agent/telemetry` |
| GET | `/openapi.json` |

## Environment

Every variable the tree reads. `tests/unit/test_single_source.py` fails on one
that is read and not declared here, and on two readers disagreeing about a
default. A blank default means absence is itself the answer.

| Variable | Default | Owner |
|---|---|---|
| `DERATE_AGENT_PORT` | `8081` | `control_plane/registry/agent.py` — the node agent's port; docker/placeholder_app.py defaults it to DERATE_PORT instead, which is a fork only reachable by running that file directly |
| `DERATE_ALLOWED_ORIGINS` | `` | `control_plane/gateway/settings.py` |
| `DERATE_ALLOW_BRIDGE` |  | `control_plane/registry/config.py` — the container refuses bridge networking unless this is set |
| `DERATE_API_KEY` | `` | `tests/load/loadtest.py` |
| `DERATE_API_TOKEN` |  | `control_plane/gateway/settings.py` — opt-in bearer token gating /api (see gateway/auth.py); unset is a no-op |
| `DERATE_BASE_URL` | `http://localhost:8088` | `tests/load/loadtest.py` |
| `DERATE_BUILD` |  | `control_plane/version.py` — stamped into the image; absent means a source checkout |
| `DERATE_CACHE_DIR` |  | `control_plane/resolver/cache.py` — also read by registry/storage.py |
| `DERATE_CHECK_BROWSER` |  | `ui/src/check/browser.mjs` — an explicit Chromium path, for a machine whose Playwright cache is somewhere the finder does not look |
| `DERATE_CHECK_ORIGIN` | `http://localhost:8088` | `ui/src/**/*.check.mjs` — which coordinator the live-API verifiers talk to |
| `DERATE_CHECK_STRICT` |  | `ui/check.mjs` — 1 makes an unmet requirement a failure rather than its own column, which is the mode for a box where the coordinator is actually up |
| `DERATE_CLUSTER_ID` |  | `control_plane/registry/config.py` |
| `DERATE_DATA` | `/data` | `docker/entrypoint.sh` — SHELL ONLY. The entrypoint bridges it onto DERATE_DATA_DIR; no Python reads it. Overriding this alone moves nothing (audit defect M-17) |
| `DERATE_DATA_DIR` |  | `control_plane/paths.py` — unset: /data when writable, else the platform application-state dir |
| `DERATE_E2E_ORIGIN` | `http://localhost:8088` | `tests/unit/test_gateway.py` — which coordinator the audio round trip launches real models on |
| `DERATE_ELECTRICITY_RATE` | `0` | `control_plane/gateway/main.py` — currency per kWh; 0 means the spend screen shows no power cost |
| `DERATE_ENTRYPOINT` |  | `docker/entrypoint.sh` |
| `DERATE_GATEWAY` |  | `ui/vite.config.ts` — dev server only: which coordinator `npm run dev` proxies to |
| `DERATE_HF_CACHE` |  | `control_plane/registry/modelcache.py` |
| `DERATE_HF_TIMEOUT` | `8.0` | `control_plane/resolver/hf.py` |
| `DERATE_HF_TOKEN` |  | `control_plane/resolver/hf.py` — gated repositories |
| `DERATE_HOST` | `0.0.0.0` | `control_plane/gateway/main.py` |
| `DERATE_HOST_RESERVE_MIB` |  | `control_plane/registry/config.py` — how much unified memory to leave the OS on a GB10 |
| `DERATE_IMAGE` | `ghcr.io/pizzaman213/derate/node:latest` | `install.sh` |
| `DERATE_INSTALL_SH` |  | `control_plane/gateway/enroll_api.py` — override the installer script the coordinator serves |
| `DERATE_JOIN` |  | `control_plane/registry/config.py` — unset: find the coordinator over mDNS |
| `DERATE_LOAD_LOGS` |  | `tests/load/harness.py` |
| `DERATE_LOG_BACKUPS` | `LOG_BACKUPS` | `control_plane/logfiles.py` — rotated files kept, per file |
| `DERATE_LOG_DIR` |  | `control_plane/paths.py` — where node.log and proxy.log are written; unset means <project root>/logs -- /opt/derate/logs in the image, which is a layer and not the volume |
| `DERATE_LOG_FILES` | `1` | `control_plane/logfiles.py` — 0 to write no files at all -- stderr and the telemetry journal are unaffected either way |
| `DERATE_LOG_LEVEL` | `INFO` | `control_plane/gateway/main.py` |
| `DERATE_LOG_MAX_BYTES` | `LOG_MAX_BYTES` | `control_plane/logfiles.py` — rotation size, per file |
| `DERATE_MPIRUN` |  | `control_plane/links/measure.py` |
| `DERATE_MPIRUN_ARGS` | `` | `control_plane/links/measure.py` |
| `DERATE_NCCL_TESTS_DIR` |  | `control_plane/links/measure.py` |
| `DERATE_NODE_ID` |  | `control_plane/registry/config.py` — unset: derived from the hostname |
| `DERATE_OFFLINE` |  | `control_plane/resolver/resolver.py` — answer from cache and fixtures, never reach the network |
| `DERATE_PORT` | `8080` | `control_plane/gateway/main.py` — the coordinator's HTTP port; DEFAULT_COORDINATOR_PORT is the same number |
| `DERATE_PULL_HEADROOM` | `0.8` | `control_plane/providers/config.py` |
| `DERATE_RESOLVER_TTL` | `24 * 3600` | `control_plane/resolver/cache.py` — seconds; a pinned commit sha never expires whatever this says |
| `DERATE_ROLE` | `auto` | `control_plane/registry/config.py` — auto | coordinator | worker; auto becomes coordinator when none is found |
| `DERATE_SGLANG_IMAGE` |  | `control_plane/deploy/flags.py` |
| `DERATE_SHELL` | `0` | `control_plane/registry/shell_config.py` — off unless explicitly enabled |
| `DERATE_SHELL_BINARY` | `DEFAULT_SHELL` | `control_plane/registry/shell_config.py` |
| `DERATE_SHELL_IDLE_S` | `1800` | `control_plane/registry/shell_config.py` |
| `DERATE_SHELL_KEY` | `` | `control_plane/registry/shell_config.py` |
| `DERATE_SHELL_ORIGINS` | `` | `control_plane/registry/shell_config.py` |
| `DERATE_SHELL_SESSION` |  | `control_plane/registry/shell.py` — set by the server into the session it spawns; not operator-facing |
| `DERATE_SPARKRUN_BIN` |  | `control_plane/deploy/sparkrun.py` — unset: whatever `sparkrun` resolves to on PATH |
| `DERATE_STUB_PROVIDER_KEY` |  | `control_plane/providers/stub.py` — the key reference the day-0 provider stub hands out |
| `DERATE_TELEMETRY` | `1` | `control_plane/telemetry/config.py` |
| `DERATE_TELEMETRY_LOG_LEVEL` | `LOG_SHIP_LEVEL` | `control_plane/telemetry/config.py` |
| `DERATE_TELEMETRY_MAX_BYTES` |  | `control_plane/telemetry/config.py` |
| `DERATE_TELEMETRY_QUIET_LOGGERS` |  | `control_plane/telemetry/config.py` |
| `DERATE_TELEMETRY_RETENTION_DAYS` | `30` | `control_plane/telemetry/config.py` |
| `DERATE_TELEMETRY_SHIP_INTERVAL_S` | `5` | `control_plane/telemetry/config.py` |
| `DERATE_TEST_AUDIO` |  | `tests/unit/test_gateway.py` — cache the tts runtime's reference clip here, so rerunning the transcription half does not launch the speech model again |
| `DERATE_TEST_NETWORK` |  | `tests/unit/test_resolver.py` — opt in to tests that reach HuggingFace |
| `DERATE_TOKEN` |  | `control_plane/registry/config.py` — unset: the coordinator generates one and persists it |
| `DERATE_TTS_DEFAULT_VOICES` | `1` | `control_plane/runtimes/tts.py` — 0 leaves an empty voice directory empty instead of fetching a starter library |
| `DERATE_TTS_IMAGE` |  | `control_plane/deploy/flags.py` |
| `DERATE_TTS_VOICE_DIR` |  | `control_plane/runtimes/tts.py` — read inside the model container, not on the node |
| `DERATE_UI_DIR` |  | `control_plane/gateway/settings.py` — the built UI to serve; absent means the API answers and no screen does |
| `DERATE_VLLM_IMAGE` |  | `control_plane/deploy/flags.py` |
