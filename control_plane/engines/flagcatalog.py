"""Which flags an engine actually accepts, asked of the engine itself.

``flags.py``'s own header states the failure this exists to end: *a knob whose
recipe_key is absent from the template is dropped in silence*. That is the
``--kv-cache-dtype`` bug -- sized narrow, budgeted narrow, served wide, with
nothing on screen to say so -- and the template is only half of it. The other
half is ``extra_args``, which reaches the runtime through a REGEX
(``_EXTRA_ARG_SAFE``) that knows the SHAPE of a flag and nothing about whether
the engine has ever heard of it. ``--max-len 4096`` passes that grammar; vLLM
exits on it.

Measured against the pinned image on 2026-09-11: derate's template emits 16
distinct vLLM flags and the image's own parser offers **400** names across 295
options. The other 384 are reachable only as unvalidated ``extra_args``.

**Asked, never copied.** Same stance as ``resolver/imageprobe.py`` for the
architecture table and ``flags.py::NCCL_TUNABLES`` for the variables NCCL
honours, and for the same reason both give: a table that claims a flag the
image does not have is a launch that clears every gate and dies at argv, and
one that refuses a flag the image accepts is a 400 on a servable
configuration. The module path this probe imports ALREADY moved once --
``vllm.entrypoints.openai.cli_args`` became
``vllm.entrypoints.launchers.cli_args`` -- which is the argument against a
hand-kept list rather than a footnote to it.

**A catalogue that could not be read is ``None``, never an empty one.** An
engine whose image is absent, or whose probe fails, must not have every flag
refused as unknown; absence is "could not be asked", exactly as
``imageprobe.probed()`` and ``SparkrunAdapter.is_running()`` treat it.
"""

from __future__ import annotations

import difflib
import json
import subprocess
from dataclasses import dataclass

#: What a probe prints, so its JSON can be found in output that also carries
#: the engine's own import chatter. Same trick as `imageprobe`'s marker.
MARKER = "derate-flagprobe:"


@dataclass(frozen=True)
class Flag:
    """One option the engine's parser declares."""

    names: tuple[str, ...]
    #: False for a bare switch (``--enforce-eager``). A value handed to one of
    #: those is an error the engine reports, not something to pass through.
    takes_value: bool = True
    nargs: str | int | None = None
    #: The parser's own ``choices``, when it has them. 38 of vLLM's 295 do.
    choices: tuple[str, ...] | None = None
    repeatable: bool = False


@dataclass(frozen=True)
class Catalogue:
    engine: str
    version: str
    flags: tuple[Flag, ...]

    @property
    def names(self) -> frozenset[str]:
        return frozenset(n for f in self.flags for n in f.names)

    def get(self, name: str) -> Flag | None:
        for f in self.flags:
            if name in f.names:
                return f
        return None

    @classmethod
    def from_payload(cls, payload: dict) -> "Catalogue":
        return cls(
            engine=str(payload["engine"]),
            version=str(payload.get("version", "")),
            flags=tuple(
                Flag(
                    names=tuple(row["names"]),
                    takes_value=bool(row.get("takes_value", True)),
                    nargs=row.get("nargs"),
                    choices=tuple(row["choices"]) if row.get("choices") else None,
                    repeatable=bool(row.get("repeatable", False)),
                )
                for row in payload.get("flags", ())
            ),
        )


def parse_probe_output(text: str) -> Catalogue | None:
    """The catalogue in *text*, or None if it does not carry one."""
    for line in (text or "").splitlines():
        if line.startswith(MARKER):
            try:
                return Catalogue.from_payload(json.loads(line[len(MARKER):]))
            except (ValueError, KeyError, TypeError):
                return None
    return None


def flag_name(token: str) -> str | None:
    """The option name in *token*, or None if it is a value rather than a flag.

    ``--flag=value`` and ``--flag`` both answer ``--flag``; the ``=`` form is
    accepted by ``_EXTRA_ARG_SAFE`` and so reaches here.
    """
    if not token.startswith("-") or token == "-" or token == "--":
        return None
    return token.split("=", 1)[0]


def flag_refusal(
    catalogue: Catalogue | None, tokens, *, runtime: str
) -> str | None:
    """Refuse operator flags this engine's own parser would reject.

    Three questions, in the order the engine itself would hit them: is the flag
    declared at all, does a bare switch have a value stapled to it, and is a
    value one of the parser's own ``choices``.

    ``None`` when the catalogue could not be read. An engine nobody could ask
    must not have every flag refused -- that would turn a missing image into
    what looks like a bad request, which is the mistake
    ``SparkrunAdapter.is_running`` was rewritten to stop making.
    """
    if catalogue is None:
        return None

    where = "%s %s" % (runtime, catalogue.version or "(unknown version)")
    unknown: list[str] = []
    problems: list[str] = []

    for token in tokens:
        name = flag_name(token)
        if name is None:
            continue
        flag = catalogue.get(name)
        if flag is None:
            unknown.append(name)
            continue
        value = token.split("=", 1)[1] if "=" in token else None
        if value is not None and not flag.takes_value:
            problems.append(
                "%s is a switch and takes no value; drop the %r." % (name, "=" + value)
            )
        elif value is not None and flag.choices and value not in flag.choices:
            problems.append(
                "%s does not accept %r. It accepts: %s."
                % (name, value, ", ".join(flag.choices))
            )

    if not unknown and not problems:
        return None

    lines: list[str] = []
    for name in unknown:
        suggestion = _nearest(name, catalogue.names)
        lines.append(
            "%s has no flag %s.%s"
            % (where, name,
               (" Did you mean %s?" % suggestion) if suggestion else "")
        )
    lines.extend(problems)
    lines.append(
        "Read off this image's own parser rather than a table kept here, so "
        "it is what the engine itself would have exited on."
    )
    return "\n".join(lines)


def _nearest(name: str, names) -> str | None:
    """The closest declared name, or None when nothing is close enough.

    ``difflib`` rather than a shared-prefix rule, because the prefix rule is
    wrong in the obvious case: ``--max-len`` shares five characters with
    ``--max-lora-rank`` and would have suggested it over ``--max-model-len``.
    A suggestion that is wrong is worse than no suggestion, so the cutoff is
    deliberately high and silence is the normal answer.
    """
    close = difflib.get_close_matches(name, sorted(names), n=1, cutoff=0.72)
    return close[0] if close else None


def probe(image: str, script: str, *, gpus: bool, docker: str = "docker",
          timeout: float = 300.0) -> Catalogue | None:
    """Run *script* inside *image* and read the catalogue back.

    Never raises: a missing docker, a missing image and a probe that crashed
    are all "could not be asked".

    *gpus* is not a preference. vLLM builds its parser through device-aware
    config and raises ``Failed to infer device type`` without a visible GPU, so
    the probe that reads its flags needs one even though it loads no model.
    """
    argv = [docker, "run", "--rm"]
    if gpus:
        argv += ["--gpus", "all"]
    argv += ["--entrypoint", "python3", image, "-c", script]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_probe_output(done.stdout) or parse_probe_output(done.stderr)
