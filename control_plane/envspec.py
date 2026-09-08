"""Every environment variable derate reads, once, with its default.

Environment variables are the one contract with no type and no import graph.
Nothing fails when a shell script exports one spelling and a Python module
reads another; the variable is simply absent and the default silently wins. They are also read from four languages here -- Python,
the entrypoint shell, the Dockerfile, compose -- so there has never been one
place to look up what exists.

The pair that shows why this file exists: ``docker/entrypoint.sh`` exports
``DERATE_DATA`` and every Python component reads ``DERATE_DATA_DIR``. The two
defaults coincide at ``/data``, so the split is invisible until somebody
overrides one of them and the other keeps pointing at the old estate. The
entrypoint bridges them by hand (``DERATE_DATA_DIR="${DERATE_DATA_DIR:-$DERATE_DATA}"``),
which works and is exactly the kind of bridge nobody remembers on the fifth
variable.

``tests/unit/test_single_source.py`` greps the tree for ``DERATE_*`` and fails on
anything not declared here or matched by a :data:`DYNAMIC` pattern, so a new
variable cannot arrive undocumented.

``default`` is what the reader falls back to spelled as the reader spells it.
``None`` means unset is a real answer -- an absent ``DERATE_TOKEN`` makes the
coordinator generate one, which is not the same as an empty string.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EnvVar:
    name: str
    #: The literal fallback, as written at the reading site. ``None`` when
    #: absence is itself the answer rather than a stand-in for a value.
    default: str | None
    #: The file that owns the read. Where to look when the default is wrong.
    owner: str
    note: str = ""


@dataclass(frozen=True)
class DynamicEnv:
    """A family of variables whose full name is not known until runtime."""

    prefix: str
    suffix: str
    owner: str
    note: str


def _v(name, default, owner, note=""):
    return EnvVar(name=name, default=default, owner=owner, note=note)


_DECLARED = (
    # -- identity, role and the ports ---------------------------------------
    _v("DERATE_ROLE", "auto", "control_plane/registry/config.py",
       "auto | coordinator | worker; auto becomes coordinator when none is found"),
    _v("DERATE_PORT", "8080", "control_plane/gateway/main.py",
       "the coordinator's HTTP port; DEFAULT_COORDINATOR_PORT is the same number"),
    _v("DERATE_AGENT_PORT", "8081", "control_plane/registry/agent.py",
       "the node agent's port; docker/placeholder_app.py defaults it to DERATE_PORT "
       "instead, which is a fork only reachable by running that file directly"),
    _v("DERATE_HOST", "0.0.0.0", "control_plane/gateway/main.py", ""),
    _v("DERATE_TOKEN", None, "control_plane/registry/config.py",
       "unset: the coordinator generates one and persists it"),
    _v("DERATE_JOIN", None, "control_plane/registry/config.py",
       "unset: find the coordinator over mDNS"),
    _v("DERATE_NODE_ID", None, "control_plane/registry/config.py",
       "unset: derived from the hostname"),
    _v("DERATE_CLUSTER_ID", None, "control_plane/registry/config.py", ""),
    _v("DERATE_BUILD", None, "control_plane/version.py",
       "stamped into the image; absent means a source checkout"),
    _v("DERATE_ALLOW_BRIDGE", None, "control_plane/registry/config.py",
       "the container refuses bridge networking unless this is set"),
    _v("DERATE_HOST_RESERVE_MIB", None, "control_plane/registry/config.py",
       "how much unified memory to leave the OS on a GB10"),

    # -- where state lives ---------------------------------------------------
    _v("DERATE_DATA_DIR", None, "control_plane/paths.py",
       "unset: /data when writable, else the platform application-state dir"),
    _v("DERATE_DATA", "/data", "docker/entrypoint.sh",
       "SHELL ONLY. The entrypoint bridges it onto DERATE_DATA_DIR; no Python "
       "reads it. Overriding this alone moves nothing (audit defect M-17)"),
    _v("DERATE_CACHE_DIR", None, "control_plane/resolver/cache.py",
       "also read by registry/storage.py"),
    _v("DERATE_UI_DIR", None, "control_plane/gateway/settings.py",
       "the built UI to serve; absent means the API answers and no screen does"),
    _v("DERATE_HF_CACHE", None, "control_plane/registry/modelcache.py", ""),

    # -- the model resolver --------------------------------------------------
    _v("DERATE_HF_TOKEN", None, "control_plane/resolver/hf.py", "gated repositories"),
    _v("DERATE_HF_TIMEOUT", "8.0", "control_plane/resolver/hf.py", ""),
    _v("DERATE_RESOLVER_TTL", "24 * 3600", "control_plane/resolver/cache.py",
       "seconds; a pinned commit sha never expires whatever this says"),
    _v("DERATE_OFFLINE", None, "control_plane/resolver/resolver.py",
       "answer from cache and fixtures, never reach the network"),

    # -- launching -----------------------------------------------------------
    _v("DERATE_VLLM_IMAGE", None, "control_plane/deploy/flags.py", ""),
    _v("DERATE_SGLANG_IMAGE", None, "control_plane/deploy/flags.py", ""),
    _v("DERATE_TTS_IMAGE", None, "control_plane/deploy/flags.py", ""),
    _v("DERATE_TTS_VOICE_DIR", None, "control_plane/runtimes/tts.py",
       "read inside the model container, not on the node"),
    _v("DERATE_SPARKRUN_BIN", None, "control_plane/deploy/sparkrun.py",
       "unset: whatever `sparkrun` resolves to on PATH"),
    _v("DERATE_IMAGE", "ghcr.io/pizzaman213/derate/node:latest", "install.sh", ""),
    _v("DERATE_ENTRYPOINT", None, "docker/entrypoint.sh", ""),

    # -- the interconnect measurement ---------------------------------------
    _v("DERATE_MPIRUN", None, "control_plane/links/measure.py", ""),
    _v("DERATE_MPIRUN_ARGS", "", "control_plane/links/measure.py", ""),
    _v("DERATE_NCCL_TESTS_DIR", None, "control_plane/links/measure.py", ""),

    # -- providers -----------------------------------------------------------
    _v("DERATE_PULL_HEADROOM", "0.8", "control_plane/providers/config.py", ""),

    # -- the shell on a node -------------------------------------------------
    _v("DERATE_SHELL", "0", "control_plane/registry/shell_config.py",
       "off unless explicitly enabled"),
    _v("DERATE_SHELL_KEY", "", "control_plane/registry/shell_config.py", ""),
    _v("DERATE_SHELL_ORIGINS", "", "control_plane/registry/shell_config.py", ""),
    _v("DERATE_SHELL_IDLE_S", "1800", "control_plane/registry/shell_config.py", ""),
    _v("DERATE_SHELL_BINARY", "DEFAULT_SHELL", "control_plane/registry/shell_config.py", ""),
    _v("DERATE_SHELL_SESSION", None, "control_plane/registry/shell.py",
       "set by the server into the session it spawns; not operator-facing"),

    # -- telemetry -----------------------------------------------------------
    _v("DERATE_TELEMETRY", "1", "control_plane/telemetry/config.py", ""),
    _v("DERATE_TELEMETRY_SHIP_INTERVAL_S", "5", "control_plane/telemetry/config.py", ""),
    _v("DERATE_TELEMETRY_RETENTION_DAYS", "30", "control_plane/telemetry/config.py", ""),
    _v("DERATE_TELEMETRY_MAX_BYTES", None, "control_plane/telemetry/config.py", ""),
    _v("DERATE_TELEMETRY_LOG_LEVEL", "LOG_SHIP_LEVEL", "control_plane/telemetry/config.py", ""),
    _v("DERATE_TELEMETRY_QUIET_LOGGERS", None, "control_plane/telemetry/config.py", ""),
    _v("DERATE_LOG_LEVEL", "INFO", "control_plane/gateway/main.py", ""),

    # -- the log folder ------------------------------------------------------
    _v("DERATE_LOG_DIR", None, "control_plane/paths.py",
       "where node.log and proxy.log are written; unset means <project root>/logs "
       "-- /opt/derate/logs in the image, which is a layer and not the volume"),
    _v("DERATE_LOG_FILES", "1", "control_plane/logfiles.py",
       "0 to write no files at all -- stderr and the telemetry journal are "
       "unaffected either way"),
    _v("DERATE_LOG_MAX_BYTES", "LOG_MAX_BYTES", "control_plane/logfiles.py",
       "rotation size, per file"),
    _v("DERATE_LOG_BACKUPS", "LOG_BACKUPS", "control_plane/logfiles.py",
       "rotated files kept, per file"),

    # -- the gateway ---------------------------------------------------------
    _v("DERATE_ALLOWED_ORIGINS", "", "control_plane/gateway/settings.py", ""),
    _v("DERATE_ELECTRICITY_RATE", "0", "control_plane/gateway/main.py",
       "currency per kWh; 0 means the spend screen shows no power cost"),
    _v("DERATE_INSTALL_SH", None, "control_plane/gateway/enroll_api.py",
       "override the installer script the coordinator serves"),
    _v("DERATE_GATEWAY", None, "ui/vite.config.ts",
       "dev server only: which coordinator `npm run dev` proxies to"),

    # -- tests, harnesses and verifiers -------------------------------------
    _v("DERATE_TEST_NETWORK", None, "tests/unit/test_resolver.py",
       "opt in to tests that reach HuggingFace"),
    _v("DERATE_LOAD_LOGS", None, "tests/load/harness.py", ""),
    _v("DERATE_STUB_PROVIDER_KEY", None, "control_plane/providers/stub.py",
       "the key reference the day-0 provider stub hands out"),
    _v("DERATE_API_KEY", "", "tests/load/loadtest.py", ""),
    _v("DERATE_BASE_URL", "http://localhost:8088", "tests/load/loadtest.py", ""),
    _v("DERATE_CHECK_ORIGIN", "http://localhost:8088", "ui/src/**/*.check.mjs",
       "which coordinator the live-API verifiers talk to"),
    _v("DERATE_CHECK_STRICT", None, "ui/check.mjs",
       "1 makes an unmet requirement a failure rather than its own column, "
       "which is the mode for a box where the coordinator is actually up"),
    _v("DERATE_CHECK_BROWSER", None, "ui/src/check/browser.mjs",
       "an explicit Chromium path, for a machine whose Playwright cache is "
       "somewhere the finder does not look"),
    _v("DERATE_TTS_DEFAULT_VOICES", "1", "control_plane/runtimes/tts.py",
       "0 leaves an empty voice directory empty instead of fetching a "
       "starter library"),
    _v("DERATE_E2E_ORIGIN", "http://localhost:8088", "tests/unit/test_gateway.py",
       "which coordinator the audio round trip launches real models on"),
    _v("DERATE_TEST_AUDIO", None, "tests/unit/test_gateway.py",
       "cache the tts runtime's reference clip here, so rerunning the "
       "transcription half does not launch the speech model again"),
)

VARIABLES: dict[str, EnvVar] = {v.name: v for v in _DECLARED}

#: Families whose full name is only known at runtime. A grep finds instances of
#: these -- `DERATE_OPENROUTER_API_KEY`, `DERATE_DATAPLANE_SPARK_01` -- which are
#: values somebody configured, not variables this project declares.
DYNAMIC: tuple[DynamicEnv, ...] = (
    DynamicEnv(
        prefix="DERATE_",
        suffix="_API_KEY",
        owner="control_plane/providers/service.py",
        note=(
            "a minted provider key reference: MINTED_REF_SUFFIX. The name is the "
            "provider's, the value is the key, and neither is ever rendered"
        ),
    ),
    DynamicEnv(
        prefix="DERATE_DATAPLANE_",
        suffix="",
        owner="control_plane/links/service.py",
        note="per-node data-plane address override, keyed by node id",
    ),
)


def is_declared(name: str) -> bool:
    """Whether *name* is a variable this project knows about.

    A name that *is* one of the :data:`DYNAMIC` prefixes counts: that is the
    literal a call site builds a real name out of
    (``f"DERATE_DATAPLANE_{key}"``), not a variable anybody sets.
    """
    if name in VARIABLES:
        return True
    for d in DYNAMIC:
        if name == d.prefix or name == d.prefix.rstrip("_"):
            return True
        if (
            name.startswith(d.prefix)
            and name.endswith(d.suffix)
            and len(name) > len(d.prefix) + len(d.suffix)
        ):
            return True
    return False
