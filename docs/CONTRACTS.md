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
| `AuditAction` | `login`, `login_failed`, `logout`, `password_changed`, `admin_added`, `admin_removed`, `setup_admin_created`, `org_created`, `org_updated`, `org_deleted`, `key_created`, `key_updated`, `key_disabled`, `key_enabled`, `key_rotated`, `key_deleted`, `limit_changed`, `limit_raised`, `cycle_reset`, `pricing_changed`, `api_write`, `shell_opened`, `shell_closed`, `provider_created`, `provider_updated`, `provider_deleted`, `settings_changed`, `enrollment_minted`, `enrollment_revoked`, `deployment_launched`, `deployment_stopped`, `node_removed`, `node_admitted`, `node_process_killed`, `routing_changed`, `storage_deleted`, `sessions_revoked`, `node_joined`, `provider_signed_in`, `login_locked`, `key_refused`, `session_refused`, `setup_refused`, `join_rejected`, `cross_origin_refused`, `session_ip_changed`, `audit_chain_broken`, `agent_refused`, `cluster_token_rotated`, `node_runtime_model_changed` | `control_plane.contracts.auth` |
| `AuthErrorCode` | `missing_api_key`, `invalid_api_key`, `api_key_disabled`, `api_key_expired`, `session_required`, `invalid_credentials`, `login_locked`, `setup_closed`, `auth_unavailable`, `limit_exceeded`, `max_output_tokens_exceeded`, `max_prompt_tokens_exceeded`, `limit_exceeds_org`, `allowlist_exceeds_org`, `model_not_allowed`, `setup_local_only` | `control_plane.contracts.auth` |
| `ContextBucket` | `0-8k`, `8-32k`, `32-128k`, `128k+` | `control_plane.contracts.telemetry` |
| `CostBasis` | `metered`, `estimated`, `unpriced` | `control_plane.contracts.telemetry` |
| `DeploymentOrigin` | `launched`, `adopted` | `control_plane.contracts.deployment` |
| `DeploymentState` | `planned`, `launching`, `ready`, `degraded`, `failed`, `stopping`, `stopped` | `control_plane.contracts.deployment` |
| `DeviceClass` | `gb10`, `discrete`, `apple`, `cpu`, `unknown` | `control_plane.contracts.hardware` |
| `FallbackReason` | `cloud_pool_exhausted`, `upstream_failed` | `control_plane.contracts.telemetry` |
| `FieldError` | `not_reported`, `ambiguous_concurrency`, `not_enabled` | `control_plane.contracts.telemetry` |
| `KeyStatus` | `active`, `disabled`, `expired` | `control_plane.contracts.auth` |
| `LimitScope` | `key`, `org`, `route` | `control_plane.contracts.auth` |
| `Modality` | `text`, `embedding`, `speech`, `transcription` | `control_plane.contracts.modality` |
| `ParallelismKind` | `single_node`, `tensor`, `pipeline`, `expert`, `hybrid` | `control_plane.contracts.plan` |
| `PrincipalVia` | `api_key`, `session` | `control_plane.contracts.auth` |
| `ProviderKind` | `openrouter`, `openai`, `anthropic`, `together`, `groq`, `ollama`, `custom`, `chatgpt`, `claude_code` | `control_plane.contracts.providers` |
| `RoutingPolicy` | `least_outstanding`, `round_robin`, `weighted_capacity`, `cache_affinity`, `failover`, `local_first`, `cost_aware` | `control_plane.contracts.routing` |
| `SpeculativeMethod` | `mtp`, `dspark`, `ngram`, `eagle`, `eagle3`, `medusa`, `dflash` | `control_plane.contracts.plan` |
| `TargetKind` | `local`, `remote` | `control_plane.contracts.routing` |
| `Verdict` | `fits`, `fits_degraded`, `wont_fit` | `control_plane.contracts.plan` |

## Data shapes

### `Admin`

`control_plane.contracts.auth`

| Field | Type | Optional |
|---|---|---|
| `username` | `str` | no |
| `email` | `str | None` | no |
| `created_at` | `float` | no |
| `created_by` | `str | None` | no |

### `ApiKey`

`control_plane.contracts.auth`

| Field | Type | Optional |
|---|---|---|
| `key_id` | `str` | no |
| `name` | `str` | no |
| `org_id` | `str` | no |
| `created_by` | `str` | no |
| `created_at` | `float` | no |
| `expires_at` | `float | None` | no |
| `status` | `KeyStatus` | no |
| `disabled_at` | `float | None` | yes |
| `disabled_by` | `str | None` | yes |
| `disabled_reason` | `str | None` | yes |
| `last_used_at` | `float | None` | yes |
| `last_used_ip` | `str | None` | yes |
| `notes` | `str` | yes |
| `is_admin_key` | `bool` | yes |
| `admin_username` | `str | None` | yes |
| `allowed_models` | `list[str] | None` | yes |
| `routing_policy` | `RoutingPolicy | None` | yes |
| `limits` | `dict[str, float]` | yes |
| `cloud_fallback_local_model` | `str | None` | yes |
| `deleted_at` | `float | None` | yes |

### `AuditEntry`

`control_plane.contracts.auth`

| Field | Type | Optional |
|---|---|---|
| `id` | `int` | no |
| `ts` | `float` | no |
| `actor` | `str` | no |
| `action` | `AuditAction` | no |
| `target_kind` | `str` | no |
| `target_id` | `str` | no |
| `before` | `dict | None` | no |
| `after` | `dict | None` | no |
| `ip` | `str | None` | no |
| `request_id` | `str | None` | yes |
| `user_agent` | `str | None` | yes |
| `session_id` | `str | None` | yes |
| `status` | `int | None` | yes |

### `ChainReport`

`control_plane.contracts.auth`

| Field | Type | Optional |
|---|---|---|
| `ok` | `bool` | no |
| `rows` | `int` | no |
| `first_bad_id` | `int | None` | no |
| `head_hash` | `str | None` | no |
| `anchor` | `str | None` | yes |

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
| `speculative` | `SpeculativeSpec | None` | yes |
| `origin` | `DeploymentOrigin` | yes |
| `enforce_eager` | `bool` | yes |
| `cudagraph_capture_sizes` | `tuple[int, ...] | None` | yes |
| `kv_dtype` | `str | None` | yes |
| `quantization` | `str | None` | yes |
| `serving` | `bool` | yes |
| `image` | `str | None` | yes |
| `revision` | `str | None` | yes |
| `speculative_tokens` | `int` | yes |
| `trust_remote_code` | `bool` | yes |
| `image_id` | `str` | yes |

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
| `speculative` | `SpeculativeSpec | None` | yes |

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
| `speculative_decode_tps_floor` | `float | None` | yes |
| `speculative_decode_tps_ceiling` | `float | None` | yes |
| `speculative_reason` | `str` | yes |
| `predicted_decode_tps_empty` | `float | None` | yes |

### `GpuProcess`

`control_plane.contracts.hardware`

| Field | Type | Optional |
|---|---|---|
| `pid` | `int` | no |
| `name` | `str` | no |
| `command` | `str | None` | no |
| `user` | `str | None` | no |
| `gpu_memory` | `int` | no |

### `LimitDecision`

`control_plane.contracts.auth`

| Field | Type | Optional |
|---|---|---|
| `allowed` | `bool` | no |
| `status` | `int` | yes |
| `code` | `str` | yes |
| `message` | `str` | yes |
| `limit_source` | `str | None` | yes |
| `used` | `float | None` | yes |
| `limit` | `float | None` | yes |
| `resets_at` | `float | None` | yes |
| `inject_max_tokens` | `int | None` | yes |
| `fallback_model` | `str | None` | yes |

### `LimitState`

`control_plane.contracts.auth`

| Field | Type | Optional |
|---|---|---|
| `limit_type` | `str` | no |
| `scope` | `LimitScope` | no |
| `scope_id` | `str` | no |
| `limit` | `float` | no |
| `effective_limit` | `float` | no |
| `used` | `float` | no |
| `remaining` | `float` | no |
| `resets_at` | `float | None` | no |
| `fraction` | `float` | no |
| `near` | `bool` | no |
| `at` | `bool` | no |

### `LinkMeasurement`

`control_plane.contracts.hardware`

| Field | Type | Optional |
|---|---|---|
| `src` | `str` | no |
| `dst` | `str` | no |
| `all_reduce_gbps` | `float` | no |
| `sendrecv_gbps` | `float` | no |
| `latency_us` | `float | None` | no |
| `gpudirect_rdma` | `bool` | no |
| `measured_at` | `float` | no |
| `method` | `str` | no |

### `LocalPrice`

`control_plane.contracts.auth`

| Field | Type | Optional |
|---|---|---|
| `model` | `str` | no |
| `input_usd_per_mtok` | `float | None` | no |
| `output_usd_per_mtok` | `float | None` | no |
| `set_by` | `str` | no |
| `set_at` | `float` | no |

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

### `MintedKey`

`control_plane.contracts.auth`

| Field | Type | Optional |
|---|---|---|
| `key` | `ApiKey` | no |
| `secret_once` | `str` | no |

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
| `routed_expert_params` | `int` | yes |
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

### `Org`

`control_plane.contracts.auth`

| Field | Type | Optional |
|---|---|---|
| `org_id` | `str` | no |
| `name` | `str` | no |
| `created_at` | `float` | no |
| `created_by` | `str` | no |
| `allowed_models` | `list[str] | None` | yes |
| `limits` | `dict[str, float]` | yes |
| `default_key_limits` | `dict[str, float]` | yes |
| `default_key_expiry_days` | `int | None` | yes |
| `routing_policy` | `RoutingPolicy | None` | yes |
| `deleted_at` | `float | None` | yes |

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

### `Principal`

`control_plane.contracts.auth`

| Field | Type | Optional |
|---|---|---|
| `key` | `ApiKey` | no |
| `org` | `Org` | no |
| `via` | `PrincipalVia` | no |
| `ip` | `str | None` | no |
| `user_agent` | `str | None` | no |

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
| `price_per_minute_usd` | `float | None` | yes |

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

### `Reservation`

`control_plane.contracts.auth`

| Field | Type | Optional |
|---|---|---|
| `reservation_id` | `str` | no |
| `key_id` | `str` | no |
| `org_id` | `str` | no |
| `model` | `str` | no |
| `started_at` | `float` | no |
| `estimated_prompt` | `int` | no |
| `estimated_output` | `int` | no |
| `slots` | `tuple[str, ...]` | yes |

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
| `runtime_api_key` | `str | None` | yes |

### `RoutingConfig`

`control_plane.contracts.routing`

| Field | Type | Optional |
|---|---|---|
| `served_name` | `str` | no |
| `policy` | `RoutingPolicy` | no |
| `targets` | `list[RouteTarget]` | yes |
| `sticky_ttl_s` | `int` | yes |
| `limits` | `dict[str, int]` | yes |
| `shares` | `dict[str, int]` | yes |

### `Session`

`control_plane.contracts.auth`

| Field | Type | Optional |
|---|---|---|
| `session_id` | `str` | no |
| `username` | `str` | no |
| `created_at` | `float` | no |
| `last_seen_at` | `float` | no |
| `expires_at` | `float` | no |
| `ip` | `str | None` | no |
| `user_agent` | `str | None` | yes |
| `login_ip` | `str | None` | yes |

### `SpeculativeSpec`

`control_plane.contracts.plan`

| Field | Type | Optional |
|---|---|---|
| `method` | `SpeculativeMethod` | no |
| `num_speculative_tokens` | `int` | no |
| `draft_bytes` | `int` | no |
| `draft_params` | `int` | no |
| `model` | `str | None` | yes |
| `draft_kv_ratio` | `float` | yes |

### `UsageActuals`

`control_plane.contracts.auth`

| Field | Type | Optional |
|---|---|---|
| `prompt_tokens` | `int` | no |
| `completion_tokens` | `int` | no |
| `cost_usd` | `float | None` | no |
| `cost_basis` | `CostBasis` | no |
| `target_kind` | `str` | no |
| `model_served` | `str` | no |

## Ports

The interfaces components hold each other to.

| Port | Methods |
|---|---|
| `AuthPort` | `admin_exists`, `audit`, `audit_page`, `change_password`, `create_admin`, `create_first_admin`, `head_hash`, `list_admins`, `login`, `login_delay_s`, `logout`, `reauthenticate`, `remove_admin`, `revoke_all`, `session`, `verify_chain` |
| `DeploymentPort` | `get`, `launch`, `list`, `stop` |
| `FitPort` | `check`, `max_context` |
| `KeyPort` | `authenticate`, `create_key`, `create_org`, `default_org_id`, `delete_key`, `delete_org`, `disable_key`, `effective_allowlist`, `enable_key`, `get_key`, `get_org`, `list_keys`, `list_orgs`, `rotate_key`, `sweep_expired`, `update_key`, `update_org`, `validate_against_org` |
| `LimitPort` | `check_and_reserve`, `check_target_kind`, `cycle_bounds`, `mark_started`, `raise_for_cycle`, `reset_cycle`, `settle`, `states` |
| `LinkPort` | `get`, `measure`, `worst_all_reduce` |
| `PlannerPort` | `plan` |
| `PricingPort` | `clear`, `cost`, `get`, `list`, `set` |
| `ProviderPort` | `add`, `health`, `list`, `models`, `refresh`, `resolve_key` |
| `RegistryPort` | `get_node`, `healthy_nodes`, `list_nodes` |
| `ResolverPort` | `resolve` |

## Constants

| Name | Value | Defined in |
|---|---|---|
| `BYTES_PER_PARAM` | (33 entries) | `control_plane.contracts.quant` |
| `COMM_BUFFER_BYTES` | `1610612736` | `control_plane.contracts.constants` |
| `CYCLE_DAYS` | `30` | `control_plane.contracts.auth` |
| `DEFAULT_DTYPE` | `bf16` | `control_plane.contracts.quant` |
| `DEFAULT_GUARDRAIL` | `0.9` | `control_plane.contracts.hardware` |
| `DEGRADED_TPS_THRESHOLD` | `10.0` | `control_plane.contracts.constants` |
| `EMAIL_MAX_LEN` | `254` | `control_plane.contracts.auth` |
| `ENDPOINT_FOR_MODALITY` | (4 entries) | `control_plane.contracts.modality` |
| `EP_EXTRA_BUFFER_BYTES` | `2147483648` | `control_plane.contracts.constants` |
| `EP_VIABLE_THRESHOLD` | `40.0` | `control_plane.contracts.constants` |
| `FRAMEWORK_OVERHEAD` | `1073741824` | `control_plane.contracts.constants` |
| `GB10_ADDRESSABLE` | `128526896332` | `control_plane.contracts.constants` |
| `GB10_MEM_BANDWIDTH` | `273.0` | `control_plane.contracts.constants` |
| `GB10_TOTAL_MEMORY` | `137438953472` | `control_plane.contracts.constants` |
| `JOIN_PER_ADDRESS_PER_MIN` | `30` | `control_plane.contracts.constants` |
| `KEY_EXPIRY_DAYS` | `[1, 10, 20, 30, 50, 100, 365]` | `control_plane.contracts.auth` |
| `KEY_ID_LEN` | `10` | `control_plane.contracts.auth` |
| `KEY_PREFIX` | `drt_` | `control_plane.contracts.auth` |
| `KEY_SECRET_BYTES` | `32` | `control_plane.contracts.auth` |
| `LIMIT_TYPES` | `['tokens_total_cycle', 'tokens_input_cycle', 'tokens_output_cycle', 'requests_cycle', 'budget_usd_cycle', 'local_tokens_cycle', 'local_usd_cycle', 'cloud_tokens_cycle', 'cloud_usd_cycle', 'rpm', 'tpm', 'max_concurrent', 'max_output_tokens_per_request', 'max_prompt_tokens_per_request', 'model:<id>:tokens_cycle', 'model:<id>:usd_cycle', 'model:<id>:rpm', 'model:<id>:tpm']` | `control_plane.contracts.auth` |
| `LOGIN_DELAY_MAX_S` | `8` | `control_plane.contracts.auth` |
| `LOGIN_INFLIGHT_PER_IP` | `2` | `control_plane.contracts.auth` |
| `LOGIN_LOCK_BASE_S` | `30` | `control_plane.contracts.auth` |
| `LOGIN_LOCK_MAX_S` | `900` | `control_plane.contracts.auth` |
| `LOGIN_LOCK_THRESHOLD` | `5` | `control_plane.contracts.auth` |
| `LOGIN_LOCK_USERNAME_MAX_S` | `300` | `control_plane.contracts.auth` |
| `LOGIN_LOCK_WINDOW_S` | `900` | `control_plane.contracts.auth` |
| `LOGIN_PASSWORD_MAX_LEN` | `1024` | `control_plane.contracts.auth` |
| `MAX_SPECULATIVE_TOKENS` | `8` | `control_plane.contracts.deployment` |
| `MODEL_LIMIT_SUFFIXES` | `['tokens_cycle', 'usd_cycle', 'rpm', 'tpm']` | `control_plane.contracts.auth` |
| `NEAR_LIMIT_FRACTION` | `0.8` | `control_plane.contracts.auth` |
| `PASSWORD_MIN_LEN` | `12` | `control_plane.contracts.auth` |
| `QUANT_INFO` | (33 entries) | `control_plane.contracts.quant` |
| `REQUEST_RECORD_FIELDS` | `['request_id', 'ts', 'served_name', 'node_id', 'target_id', 'target_kind', 'provider_id', 'deployment_id', 'policy', 'strength_source', 'attempt_no', 'attempts', 'retry_reason', 'status', 'error_code', 'error_class', 'prompt_tokens', 'completion_tokens', 'tokens', 'tokens_estimated', 'cost_usd', 'ttft_ms', 'decode_ms', 'duration_ms', 'parked_ms', 'streaming', 'body_bytes', 'admission_code', 'kv_bytes', 'key_id', 'key_name', 'org_id', 'client_ip', 'user_agent', 'model_requested', 'model_served', 'attempt_chain', 'fallback_used', 'fallback_reason', 'finish_reason', 'cancelled', 'limit_source', 'generation_ms', 'tokens_per_s', 'tpot_mean_ms', 'tpot_p95_ms', 'tail_ms', 'reasoning_tokens', 'cache_read_tokens', 'cache_write_tokens', 'max_tokens_requested', 'message_count', 'context_bucket', 'prefix_cache_hit_rate', 'cache_error', 'spec_num_drafts', 'spec_draft_tokens', 'spec_accepted_tokens', 'spec_acceptance_rate', 'spec_mean_accepted_len', 'spec_accepted_per_position', 'spec_decode_error', 'cost_basis', 'client_port']` | `control_plane.contracts.telemetry` |
| `SESSION_COOKIE` | `derate_session` | `control_plane.contracts.auth` |
| `SESSION_COOKIE_SECURE` | `__Host-derate_session` | `control_plane.contracts.auth` |
| `SESSION_IDLE_S` | `604800` | `control_plane.contracts.auth` |
| `SESSION_MAX_S` | `2592000` | `control_plane.contracts.auth` |
| `TP_VIABLE_THRESHOLD` | `40.0` | `control_plane.contracts.constants` |
| `UNMEASURED_COLLECTIVE_LATENCY_US` | `40.0` | `control_plane.contracts.constants` |
| `USAGE_BUCKETS` | `['1m', '1h', '1d']` | `control_plane.contracts.telemetry` |
| `USAGE_GROUP_BY` | `['key_id', 'org_id', 'model', 'node', 'provider', 'status', 'context_bucket']` | `control_plane.contracts.telemetry` |
| `VERIFY_INFLIGHT_MAX` | `4` | `control_plane.contracts.auth` |

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

### `drt_key_pattern`

- **Canonical**: `control_plane.keys.format:KEY_PATTERN` = `\bdrt_[a-z0-9]{10}_[A-Za-z0-9_\-]{32,}`
- **Why not in `contracts/`**: the key format belongs to the keys package; redaction.py may import nothing from it (every node, including the worker path, imports the scrubber), so it holds a copy of the pattern it scrubs by. ui/src/api/redact.ts DRT_KEY is a third, TypeScript, copy
- **Checked copies**: `control_plane.redaction:KEY_PATTERN`
- **Restated (not checkable)**: `ui/src/api/redact.ts`, `ui/src/shell/screens.check.mjs`

### `login_lock_username_cap`

- **Canonical**: `control_plane.contracts.auth:LOGIN_LOCK_USERNAME_MAX_S` = `300`
- **Why not in `contracts/`**: a policy about the lockout rather than a shape: anyone can type a username, so the bare-username lock is a denial of service on the real admin and is capped short. auth/service.py enforces it; the auth README and the forensics runbook state the number
- **Restated (not checkable)**: `control_plane/auth/README.md`, `docs/FORENSICS.md`

### `node_roles`

- **Canonical**: `control_plane.registry.config:ROLE_COORDINATOR` = `coordinator`
- **Why not in `contracts/`**: the role a node takes is the registry's question to answer

### `redacted_placeholder`

- **Canonical**: `control_plane.redaction:REDACTED` = `***`
- **Why not in `contracts/`**: it belongs with the scrubber that writes it, and that scrubber is no longer inside the provider package -- logfiles.py redacts on every node, and the worker path may not import providers
- **Checked copies**: `control_plane.providers.config:REDACTED`, `control_plane.gateway.serialize:REDACTED`

### `request_record_fields`

- **Canonical**: `control_plane.contracts.telemetry:REQUEST_RECORD_FIELDS` = `request_id`, `ts`, `served_name`, `node_id`, `target_id`, `target_kind`, `provider_id`, `deployment_id`, `policy`, `strength_source`, `attempt_no`, `attempts`, `retry_reason`, `status`, `error_code`, `error_class`, `prompt_tokens`, `completion_tokens`, `tokens`, `tokens_estimated`, `cost_usd`, `ttft_ms`, `decode_ms`, `duration_ms`, `parked_ms`, `streaming`, `body_bytes`, `admission_code`, `kv_bytes`, `key_id`, `key_name`, `org_id`, `client_ip`, `user_agent`, `model_requested`, `model_served`, `attempt_chain`, `fallback_used`, `fallback_reason`, `finish_reason`, `cancelled`, `limit_source`, `generation_ms`, `tokens_per_s`, `tpot_mean_ms`, `tpot_p95_ms`, `tail_ms`, `reasoning_tokens`, `cache_read_tokens`, `cache_write_tokens`, `max_tokens_requested`, `message_count`, `context_bucket`, `prefix_cache_hit_rate`, `cache_error`, `spec_num_drafts`, `spec_draft_tokens`, `spec_accepted_tokens`, `spec_acceptance_rate`, `spec_mean_accepted_len`, `spec_accepted_per_position`, `spec_decode_error`, `cost_basis`, `client_port`
- **Why not in `contracts/`**: the record is a dataclass in telemetry/, the column list is a contract the archive schema, the usage API and the UI read
- **Checked copies**: `control_plane.telemetry.records:REQUEST_RECORD_FIELDS`

### `runtime_names`

- **Canonical**: `control_plane.deploy.flags:SUPPORTED_RUNTIMES` = `vllm`, `sglang`, `tts`, `llamacpp`
- **Why not in `contracts/`**: the launcher owns which runtimes exist, because a runtime without a RuntimeSpec cannot be launched whatever else claims to know it
- **Checked copies**: `control_plane.resolver.support:RUNTIMES`

## HTTP surface

### Coordinator gateway (135 routes)

| Method | Path |
|---|---|
| GET | `/api/activity` |
| GET | `/api/admins` |
| POST | `/api/admins` |
| DELETE | `/api/admins/{username}` |
| DELETE | `/api/admins/{username}/sessions` |
| GET | `/api/alerts` |
| GET | `/api/audit` |
| GET | `/api/audit/integrity` |
| POST | `/api/auth/login` |
| POST | `/api/auth/logout` |
| GET | `/api/auth/me` |
| POST | `/api/auth/password` |
| DELETE | `/api/auth/sessions` |
| GET | `/api/auth/sessions` |
| GET | `/api/auth/state` |
| GET | `/api/capacity` |
| GET | `/api/catalog` |
| POST | `/api/chat/completions` |
| GET | `/api/chat/models` |
| POST | `/api/chat/speech` |
| POST | `/api/chat/transcriptions` |
| GET | `/api/chat/voices` |
| GET | `/api/cluster` |
| POST | `/api/cluster/token/rotate` |
| GET | `/api/deployments` |
| POST | `/api/deployments` |
| DELETE | `/api/deployments/{deployment_id}` |
| GET | `/api/deployments/{deployment_id}` |
| PATCH | `/api/deployments/{deployment_id}` |
| GET | `/api/deployments/{deployment_id}/logs` |
| GET | `/api/enroll` |
| POST | `/api/enroll` |
| DELETE | `/api/enroll/{token_id}` |
| GET | `/api/history/events` |
| GET | `/api/history/logs` |
| GET | `/api/history/nodes` |
| GET | `/api/history/requests` |
| GET | `/api/history/status` |
| GET | `/api/keys` |
| POST | `/api/keys` |
| DELETE | `/api/keys/{key_id}` |
| GET | `/api/keys/{key_id}` |
| PATCH | `/api/keys/{key_id}` |
| POST | `/api/keys/{key_id}/disable` |
| POST | `/api/keys/{key_id}/enable` |
| POST | `/api/keys/{key_id}/raise` |
| GET | `/api/keys/{key_id}/requests` |
| POST | `/api/keys/{key_id}/reset-cycle` |
| POST | `/api/keys/{key_id}/rotate` |
| GET | `/api/links` |
| POST | `/api/links/measure` |
| POST | `/api/links/reach` |
| POST | `/api/links/tune` |
| GET | `/api/memory` |
| GET | `/api/metrics/stream` |
| GET | `/api/models` |
| GET | `/api/models/detail` |
| GET | `/api/models/quant-table` |
| GET | `/api/models/search` |
| GET | `/api/models/speculative-heads` |
| GET | `/api/models/variants` |
| GET | `/api/nodes` |
| GET | `/api/nodes/candidates` |
| POST | `/api/nodes/join` |
| DELETE | `/api/nodes/{node_id}` |
| GET | `/api/nodes/{node_id}` |
| POST | `/api/nodes/{node_id}/admit` |
| PUT | `/api/nodes/{node_id}/label` |
| GET | `/api/nodes/{node_id}/logs` |
| GET | `/api/nodes/{node_id}/memory` |
| GET | `/api/nodes/{node_id}/processes` |
| DELETE | `/api/nodes/{node_id}/processes/{pid}` |
| GET | `/api/nodes/{node_id}/runtime` |
| POST | `/api/nodes/{node_id}/runtime` |
| POST | `/api/nodes/{node_id}/runtime/model` |
| GET | `/api/openapi.json` |
| GET | `/api/orgs` |
| POST | `/api/orgs` |
| DELETE | `/api/orgs/{org_id}` |
| GET | `/api/orgs/{org_id}` |
| PATCH | `/api/orgs/{org_id}` |
| POST | `/api/orgs/{org_id}/keys/disable` |
| GET | `/api/orgs/{org_id}/limits` |
| POST | `/api/plan` |
| GET | `/api/pricing` |
| DELETE | `/api/pricing/{model:path}` |
| GET | `/api/pricing/{model:path}` |
| PUT | `/api/pricing/{model:path}` |
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
| GET | `/api/requests` |
| GET | `/api/routing` |
| DELETE | `/api/routing/{served_name}` |
| PUT | `/api/routing/{served_name}` |
| GET | `/api/settings` |
| PATCH | `/api/settings` |
| GET | `/api/setup` |
| POST | `/api/setup/admin` |
| POST | `/api/setup/complete` |
| GET | `/api/shell/status` |
| POST | `/api/sign-in/chatgpt` |
| GET | `/api/sign-in/chatgpt/{attempt_id}` |
| POST | `/api/sign-in/chatgpt/{attempt_id}/complete` |
| POST | `/api/sign-in/claude` |
| GET | `/api/sign-in/claude/{attempt_id}` |
| POST | `/api/sign-in/claude/{attempt_id}/complete` |
| GET | `/api/storage` |
| DELETE | `/api/storage/cache/resolver` |
| DELETE | `/api/storage/nodes/{node_id}/models/{folder}` |
| GET | `/api/topology` |
| GET | `/api/usage` |
| POST | `/api/wiring` |
| GET | `/auth/callback` |
| GET | `/healthz` |
| GET | `/install.sh` |
| POST | `/v1/audio/speech` |
| POST | `/v1/audio/transcriptions` |
| GET | `/v1/audio/voices` |
| POST | `/v1/chat/completions` |
| POST | `/v1/completions` |
| POST | `/v1/embeddings` |
| GET | `/v1/key` |
| GET | `/v1/models` |
| WEBSOCKET | `/v1/realtime` |

### Node agent (12 routes)

| Method | Path |
|---|---|
| GET | `/agent/containers` |
| GET | `/agent/health` |
| GET | `/agent/journal` |
| GET | `/agent/logs` |
| GET | `/agent/models/cache` |
| DELETE | `/agent/models/cache/{folder}` |
| GET | `/agent/processes` |
| POST | `/agent/processes/{pid}/kill` |
| GET | `/agent/profile` |
| POST | `/agent/reach` |
| GET | `/agent/storage` |
| GET | `/agent/telemetry` |

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
| `DERATE_AUDIT_LOG_DAYS` | `400` | `control_plane/auditlog.py` — days of logs/audit/*.jsonl kept on disk |
| `DERATE_AUDIT_SINK` | `` | `control_plane/auditlog.py` — remote copy of every audit row: udp://host:port or tcp://host:port (RFC 5424 syslog), http(s)://... (NDJSON POST); empty = no remote copy |
| `DERATE_AUDIT_SINK_TOKEN` | `` | `control_plane/auditlog.py` — bearer token sent to an http(s) audit sink; never rendered |
| `DERATE_AUTO_ADOPT_CONTAINERS` | `1` | `control_plane/gateway/settings.py` — 0 disables the background scan that reconstructs a deployment record for a derate-launched sparkrun container running with no matching entry in the deployment store |
| `DERATE_AUTO_ADOPT_RUNTIMES` | `1` | `control_plane/gateway/settings.py` — 0 disables the background scan that registers a detected external runtime (e.g. Ollama) as a provider without a human clicking Adopt; the manual POST /api/nodes/{id}/runtime route is unaffected either way |
| `DERATE_BASE_URL` | `http://localhost:8088` | `tests/load/loadtest.py` |
| `DERATE_BUILD` |  | `control_plane/version.py` — stamped into the image; absent means a source checkout |
| `DERATE_CACHE_DIR` |  | `control_plane/resolver/cache.py` — also read by registry/storage.py |
| `DERATE_CHECK_ADMIN_PASSWORD` | `` | `ui/src/check/auth.mjs` — that admin's password |
| `DERATE_CHECK_ADMIN_USER` | `` | `ui/src/check/auth.mjs` — the admin the live-API verifiers log in as; the gateway answers /api only to a session |
| `DERATE_CHECK_API_KEY` | `` | `ui/src/check/auth.mjs` — a drt_ key for verifiers that call /v1 directly |
| `DERATE_CHECK_BROWSER` |  | `ui/src/check/browser.mjs` — an explicit Chromium path, for a machine whose Playwright cache is somewhere the finder does not look |
| `DERATE_CHECK_ORIGIN` | `http://localhost:8088` | `ui/src/**/*.check.mjs` — which coordinator the live-API verifiers talk to |
| `DERATE_CHECK_STRICT` |  | `ui/check.mjs` — 1 makes an unmet requirement a failure rather than its own column, which is the mode for a box where the coordinator is actually up |
| `DERATE_CLUSTER_ID` |  | `control_plane/registry/config.py` |
| `DERATE_DATA` | `/data` | `docker/entrypoint.sh` — SHELL ONLY. The entrypoint bridges it onto DERATE_DATA_DIR; no Python reads it. Overriding this alone moves nothing (audit defect M-17) |
| `DERATE_DATA_DIR` |  | `control_plane/paths.py` — unset: /data when writable, else the platform application-state dir |
| `DERATE_E2E_ORIGIN` | `http://localhost:8088` | `tests/unit/test_gateway.py` — which coordinator the audio round trip launches real models on |
| `DERATE_ELECTRICITY_RATE` | `0` | `control_plane/gateway/main.py` — currency per kWh; 0 means the spend screen shows no power cost |
| `DERATE_ENTRYPOINT` |  | `docker/entrypoint.sh` |
| `DERATE_GALLERY_ONLY` |  | `.omp/tests/ui/layout.check.mjs` — comma-separated component names; run.mjs sets it to scope the gallery layout matrix to the components a ui:check path names |
| `DERATE_GATEWAY` |  | `ui/vite.config.ts` — dev server only: which coordinator `npm run dev` proxies to |
| `DERATE_HF_CACHE` |  | `control_plane/registry/modelcache.py` |
| `DERATE_HF_TIMEOUT` | `8.0` | `control_plane/resolver/hf.py` |
| `DERATE_HF_TOKEN` |  | `control_plane/resolver/hf.py` — gated repositories |
| `DERATE_HOST` | `0.0.0.0` | `control_plane/gateway/main.py` |
| `DERATE_HOST_RESERVE_MIB` |  | `control_plane/registry/config.py` — how much unified memory to leave the OS on a GB10 |
| `DERATE_IMAGE` | `ghcr.io/pizzaman213/derate/node:latest` | `install.sh` |
| `DERATE_IMAGE_DIGEST` |  | `control_plane/gateway/enroll_api.py` — the sha256 digest this node's image was installed by; install.sh --digest sets it, and the join command then pins it |
| `DERATE_INSTALL_SH` |  | `control_plane/gateway/enroll_api.py` — override the installer script the coordinator serves |
| `DERATE_JOIN` |  | `control_plane/registry/config.py` — unset: find the coordinator over mDNS |
| `DERATE_LLAMACPP_IMAGE` |  | `control_plane/deploy/flags.py` — the CPU runtime's image; the default is a CPU build, so a CUDA llama.cpp tag has to be named here to be used |
| `DERATE_LOAD_LOGS` |  | `tests/load/harness.py` |
| `DERATE_LOG_BACKUPS` | `LOG_BACKUPS` | `control_plane/logfiles.py` — rotated files kept, per file |
| `DERATE_LOG_DIR` |  | `control_plane/paths.py` — where node.log, proxy.log and audit/ are written; unset means <data dir>/logs, which is on the volume and survives docker rm |
| `DERATE_LOG_FILES` | `1` | `control_plane/logfiles.py` — 0 to write no files at all -- stderr and the telemetry journal are unaffected either way |
| `DERATE_LOG_LEVEL` | `INFO` | `control_plane/gateway/main.py` |
| `DERATE_LOG_MAX_BYTES` | `LOG_MAX_BYTES` | `control_plane/logfiles.py` — rotation size, per file |
| `DERATE_MODEL_DIRS` |  | `control_plane/resolver/resolver.py` — colon-separated directories a local model path may resolve under, beside the HF cache and the data dir; anything else is refused |
| `DERATE_MPIRUN` |  | `control_plane/links/measure.py` |
| `DERATE_MPIRUN_ARGS` | `` | `control_plane/links/measure.py` |
| `DERATE_NCCL_AUTOCALIBRATE` | `1` | `control_plane/links/service.py` — 0 stops a link measurement also calibrating a never-calibrated pair. Calibration costs one two-rank collective per candidate setting and runs only when nothing is stored for that pair and image, so it is once per pair per image rather than per measurement |
| `DERATE_NCCL_ENV` |  | `control_plane/deploy/manager.py` — NAME=VALUE,... rendered into a multi-rank launch's recipe env: block. Every name is checked against the image's own NCCL_TUNABLES first, because NCCL ignores an unknown one silently. No default on purpose: nothing has measured a collective on this fabric, and NCCL already picks a protocol by message size. See tests/nccl_sweep.py |
| `DERATE_NCCL_MASTER` | `127.0.0.1` | `tests/nccl_sweep.py` — rendezvous address for the two-rank collective harness |
| `DERATE_NCCL_PORT` | `29555` | `tests/nccl_sweep.py` — rendezvous port for the same; not a served port |
| `DERATE_NCCL_TESTS_DIR` |  | `control_plane/links/measure.py` |
| `DERATE_NODE_ID` |  | `control_plane/registry/config.py` — unset: derived from the hostname |
| `DERATE_OFFLINE` |  | `control_plane/resolver/resolver.py` — answer from cache and fixtures, never reach the network |
| `DERATE_PORT` | `8080` | `control_plane/gateway/main.py` — the coordinator's HTTP port; DEFAULT_COORDINATOR_PORT is the same number |
| `DERATE_PUBLIC_ORIGIN` | `` | `control_plane/gateway/settings.py` — the origin browsers use when a TLS terminator fronts the gateway, e.g. https://derate.example; drives the cookie's Secure flag and CSRF |
| `DERATE_PULL_HEADROOM` | `0.8` | `control_plane/providers/config.py` |
| `DERATE_REQUESTS_RETENTION_DAYS` | `90` | `control_plane/telemetry/config.py` — days the archive keeps requests and access rows; not scaled by DERATE_TELEMETRY_RETENTION_DAYS |
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
| `DERATE_TELEMETRY` | `1` | `control_plane/telemetry/config.py` — 0 switches metrics, events and log capture off; /v1 request rows and access rows are evidence and are still recorded |
| `DERATE_TELEMETRY_LOG_LEVEL` | `LOG_SHIP_LEVEL` | `control_plane/telemetry/config.py` |
| `DERATE_TELEMETRY_MAX_BYTES` |  | `control_plane/telemetry/config.py` |
| `DERATE_TELEMETRY_QUIET_LOGGERS` |  | `control_plane/telemetry/config.py` |
| `DERATE_TELEMETRY_RETENTION_DAYS` | `30` | `control_plane/telemetry/config.py` |
| `DERATE_TELEMETRY_SHIP_INTERVAL_S` | `5` | `control_plane/telemetry/config.py` |
| `DERATE_TEST_AUDIO` |  | `tests/unit/test_gateway.py` — cache the tts runtime's reference clip here, so rerunning the transcription half does not launch the speech model again |
| `DERATE_TEST_NETWORK` |  | `tests/unit/test_resolver.py` — opt in to tests that reach HuggingFace |
| `DERATE_TOKEN` |  | `control_plane/registry/config.py` — unset: the coordinator generates one and persists it |
| `DERATE_TRUSTED_PROXIES` | `` | `control_plane/gateway/settings.py` — comma-separated IPs/CIDRs allowed to set X-Forwarded-For/-Proto; empty = proxy headers ignored and the peer address is the client |
| `DERATE_TTS_API_KEY` |  | `control_plane/runtimes/tts.py` — set by the recipe: the bearer the tts runtime requires on /v1; unset means the runtime answers unauthenticated |
| `DERATE_TTS_DEFAULT_VOICES` | `1` | `control_plane/runtimes/tts.py` — 0 leaves an empty voice directory empty instead of fetching a starter library |
| `DERATE_TTS_IMAGE` |  | `control_plane/deploy/flags.py` |
| `DERATE_TTS_VOICE_DIR` |  | `control_plane/runtimes/tts.py` — read inside the model container, not on the node |
| `DERATE_UI_DIR` |  | `control_plane/gateway/settings.py` — the built UI to serve; absent means the API answers and no screen does |
| `DERATE_VLLM_IMAGE` |  | `control_plane/deploy/flags.py` |
