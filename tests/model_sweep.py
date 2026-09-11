"""Resolve a lot of models at once and say which ones this build cannot read.

Two corpora, because they answer two different questions.

``--corpus`` (the default) reads every ``tests/resolver_data/*.config.json``
and compares the result against ``EXPECTED.json`` beside them. Hermetic, about
a second, and it is what ``tests/unit/test_model_corpus.py`` gates the suite on.
This is the regression net: a config that resolves today has to keep resolving,
with the same numbers.

``--live`` walks the coordinator's own catalogue -- every model it knows about,
however it learned of it -- and asks it to resolve each one. That is hundreds
of models and a network fetch behind each miss, so it is a report you run, not
a test that runs itself.

    python3 -m tests.model_sweep                  # the checked-in corpus
    python3 -m tests.model_sweep --live           # everything the box knows
    python3 -m tests.model_sweep --live --limit 50
    python3 -m tests.model_sweep --regenerate     # rewrite EXPECTED.json

**What counts as a failure is deliberately narrow.** Only a model this build
cannot read a shape out of. A model that resolves and is then refused by every
runtime is reported and not flagged: that is the support table doing its job,
and an ASR export or an embedding model belongs in that column. Widening the
net would bury the one signal that means "derate has a bug" under a list of
checkpoints that were never servable.

The live sweep separates the causes for the same reason. A gated repository, a
404 and a network wobble are facts about the hub, not about this build, and
they are counted apart from the shapes that genuinely could not be read.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib import error, parse, request

DATA = Path(__file__).parent / "resolver_data"
EXPECTED = DATA / "EXPECTED.json"

#: Substrings that mean the hub would not tell us, rather than that this build
#: could not read what it was told. Kept as literals because the resolver's own
#: sentences are the product and are not going to be given error codes just for
#: this.
_HUB_FAULT = (
    "gated", "401", "403", "404", "could not be found", "not found",
    "metadata could not be", "timed out", "connection", "temporarily",
)


@dataclass
class Outcome:
    """One model's result, in the two columns that matter."""

    model_id: str
    resolved: bool
    shape: tuple[int, int, int, int] | None = None
    experts: int = 0
    #: The rest of the numbers the fit gate reads. Kept whole rather than
    #: sampled: twice now a real change to one of these has gone through the
    #: gate untouched because the record only held the four shape figures --
    #: DiffusionGemma's `top_k_experts` was read as 2 instead of 8, a
    #: four-fold understatement of active parameters, and nothing failed.
    fit_fields: dict | None = None
    architectures: tuple[str, ...] = ()
    runtimes: dict[str, str] | None = None
    reason: str = ""
    #: Offered by a provider and not present on this cluster. Such a model has
    #: no HuggingFace repo to read a shape out of -- Amazon Nova is served over
    #: somebody's API and has no weights anywhere near here -- so "no local
    #: shape" is the correct answer for it, not a missing one.
    remote: bool = False

    @property
    def hub_fault(self) -> bool:
        low = self.reason.lower()
        return any(mark in low for mark in _HUB_FAULT)

    @property
    def unreadable(self) -> bool:
        """The only thing this sweep flags: a shape this build could not read.

        A remote model is excluded by kind rather than by its error text. It
        was never going to have a local shape, so counting it here would be
        counting a design decision as a bug -- and on this box that would have
        been 429 of the 518 models in the catalogue.
        """
        return not self.resolved and not self.hub_fault and not self.remote

    @property
    def servable(self) -> bool:
        return bool(self.runtimes) and any(
            level == "supported" for level in (self.runtimes or {}).values()
        )


# --------------------------------------------------------------------------
# The checked-in corpus.
# --------------------------------------------------------------------------


def corpus_names() -> list[str]:
    return sorted(p.name[: -len(".config.json")] for p in DATA.glob("*.config.json"))


def resolve_corpus(name: str, probe=None) -> Outcome:
    """One fixture config through the real mapper.

    With *probe* None the static tables answer, and that is what `EXPECTED.json`
    records: with a probe in play the runtime columns would describe whatever
    image happened to be pulled on the box running the suite, and the
    expectations file would be a fact about one laptop rather than about this
    checkout.

    Passing a probe runs the same corpus down the path a real coordinator
    actually uses, which is the half the static pass cannot see. Shapes must
    come out identical either way -- the mapper never consults a runtime -- and
    `compare_probed` is where that is asserted rather than assumed.
    """
    from control_plane.resolver import support
    from control_plane.resolver.config_map import map_config

    support.clear_probes()
    if probe is not None:
        support.record_probe(probe)
    config = json.loads((DATA / f"{name}.config.json").read_text())
    architectures = tuple(config.get("architectures") or ())
    try:
        mapped = map_config(config)
    except Exception as exc:
        return Outcome(name, False, architectures=architectures, reason=str(exc))

    dtype = str(config.get("torch_dtype") or config.get("dtype") or "bf16")
    if dtype not in ("fp32", "fp16", "bf16", "fp8"):
        dtype = "bf16"
    verdict = support.build_verdict(tuple(mapped.architectures or architectures), dtype)
    return Outcome(
        name,
        True,
        shape=(
            mapped.num_layers,
            mapped.hidden_size,
            mapped.num_attention_heads,
            mapped.num_kv_heads,
        ),
        experts=mapped.num_experts,
        fit_fields=_fit_fields(mapped),
        architectures=architectures,
        runtimes={e.runtime: e.level.value for e in verdict.runtimes},
    )


#: Everything `control_plane/fit` reads off a shape, plus what decides
#: parameter count. A number here being wrong is a launch refused that would
#: have fit, or admitted that will not -- which is the whole product.
_FIT_FIELDS = (
    "head_dim", "intermediate_size", "gated_mlp", "tie_word_embeddings",
    "vocab_size", "num_experts_per_token", "moe_intermediate_size",
    "num_shared_experts", "shared_expert_intermediate_size",
    "sliding_window", "layers_with_full_attention", "num_nextn_predict_layers",
    "mla_latent_dim", "kv_lora_rank", "qk_nope_head_dim", "qk_rope_head_dim",
    "v_head_dim",
)


def _fit_fields(mapped) -> dict:
    return {name: getattr(mapped, name, None) for name in _FIT_FIELDS}


def corpus_expectations() -> dict[str, dict]:
    """What every fixture should do, as a plain dict.

    Generated by ``--regenerate`` and read back by the gate, in the same shape
    as the contracts manifest: the file is the reviewed record, and the test
    fails when the code stops matching it. Reviewing a diff here is how a
    change to the mapper gets looked at rather than absorbed.
    """
    out: dict[str, dict] = {}
    for name in corpus_names():
        found = resolve_corpus(name)
        if found.resolved:
            out[name] = {
                "shape": list(found.shape),
                "experts": found.experts,
                "fit": found.fit_fields,
            }
        else:
            out[name] = {"refused": _refusal_key(found.reason)}
    return out


@dataclass
class Drift:
    """How the corpus disagrees with the record, split by which way it went.

    Both directions have to stop a green build -- a change nobody looked at is
    the thing this guards against, and "it got better" is still a change. But
    they are not the same event and reading them as one list buries the
    difference: a regression is a bug to fix, an improvement is a diff to
    review and regenerate. This session produced both within an hour.
    """

    regressions: list[str]
    improvements: list[str]

    def __bool__(self) -> bool:
        return bool(self.regressions or self.improvements)

    @property
    def all(self) -> list[str]:
        return self.regressions + self.improvements

    def render(self) -> str:
        lines = []
        if self.regressions:
            lines.append(f"{len(self.regressions)} REGRESSION(S) -- a model got worse:")
            lines += [f"  {line}" for line in self.regressions]
        if self.improvements:
            lines.append(
                f"{len(self.improvements)} improvement(s) -- a model got better. "
                f"Review, then `python3 -m tests.model_sweep --regenerate`:"
            )
            lines += [f"  {line}" for line in self.improvements]
        return "\n".join(lines)


def compare_corpus() -> Drift:
    """Every way the fixtures disagree with `EXPECTED.json`.

    Deviation is the failure here, not refusal. Two fixtures are *supposed* to
    be unreadable -- a CTranslate2 export and a static embedding model, neither
    of which has a transformer in it -- and a sweep that flagged them would cry
    wolf on every run. What must not happen is a config changing outcome
    without anyone looking at it, in either direction.
    """
    if not EXPECTED.exists():
        return Drift([], [
            f"{EXPECTED.name} does not exist; run "
            f"`python3 -m tests.model_sweep --regenerate` and review the diff"
        ])
    expected = json.loads(EXPECTED.read_text())
    regressions: list[str] = []
    improvements: list[str] = []

    for name in corpus_names():
        want = expected.get(name)
        if want is None:
            # Neither direction: nobody ever said what this should do. It is
            # a fixture that runs and asserts nothing, which is worse than
            # either kind of drift.
            regressions.append(
                f"{name}: no entry in {EXPECTED.name}. A config was added to the "
                f"corpus without recording what it should do -- regenerate and "
                f"review the diff, rather than letting it pass untested."
            )
            continue
        found = resolve_corpus(name)
        if "refused" in want:
            if found.resolved:
                # A model this build used to refuse and now reads. Every one
                # of these so far has been a real fix.
                improvements.append(
                    f"{name}: was refused ({want['refused']}), now resolves to "
                    f"{list(found.shape)} experts={found.experts}"
                )
            elif _refusal_key(found.reason) != want["refused"]:
                regressions.append(
                    f"{name}: still refused, but for a different reason.\n"
                    f"      expected: {want['refused']}\n"
                    f"      got:      {_refusal_key(found.reason)}"
                )
            continue
        if not found.resolved:
            regressions.append(
                f"{name}: expected shape {want['shape']} and it is now refused.\n"
                f"      {found.reason[:160]}"
            )
            continue
        want_fit = want.get("fit") or {}
        got_fit = found.fit_fields or {}
        moved = sorted(
            k for k in set(want_fit) | set(got_fit)
            if want_fit.get(k) != got_fit.get(k)
        )
        if moved:
            regressions.append(
                f"{name}: {len(moved)} fit input(s) changed.\n"
                + "\n".join(
                    f"      {k}: {want_fit.get(k)!r} -> {got_fit.get(k)!r}" for k in moved
                )
            )
            continue
        if list(found.shape) != want["shape"] or found.experts != want.get("experts", 0):
            # A shape that moved is a regression whichever way it moved. There
            # is no "better" number here -- either the old one was wrong and
            # the fit gate has been lying, or the new one is, and both want a
            # person rather than a regenerate.
            regressions.append(
                f"{name}: shape changed.\n"
                f"      expected: {want['shape']} experts={want.get('experts', 0)}\n"
                f"      got:      {list(found.shape)} experts={found.experts}"
            )

    for name in sorted(set(expected) - set(corpus_names())):
        regressions.append(
            f"{name}: in {EXPECTED.name} but the config file is gone. Regenerate."
        )
    return Drift(regressions, improvements)


def compare_probed(*, cache_dir: Path | None = None) -> tuple[list[str], list[str]] | None:
    """Run the corpus again with the image's registry in play.

    Returns ``(problems, gained)`` or None when the image cannot be read here.

    Two different things are being checked. `problems` is the invariant: a
    shape must not depend on which runtime image is installed, because the
    mapper never asks one. If that ever fails, something has reached across a
    boundary it has no business crossing. `gained` is information -- the models
    the pinned image can serve that the static table alone would refuse -- and
    it is the same gap that had `Gemma4ForConditionalGeneration` returning a
    400 for weeks.
    """
    from control_plane.resolver import imageprobe

    probe = imageprobe.probe("vllm", vllm_image(), cache_dir=cache_dir)
    if probe is None:
        return None

    problems: list[str] = []
    gained: list[str] = []
    for name in corpus_names():
        static = resolve_corpus(name)
        probed = resolve_corpus(name, probe=probe)
        if static.resolved != probed.resolved or static.shape != probed.shape:
            problems.append(
                f"{name}: the shape changed when a runtime probe was recorded.\n"
                f"      static: resolved={static.resolved} {static.shape}\n"
                f"      probed: resolved={probed.resolved} {probed.shape}\n"
                f"      A shape must not depend on which image is installed."
            )
            continue
        if not static.resolved:
            continue
        if not static.servable and probed.servable:
            gained.append(f"{name}: refused by the static table, loadable by the image")
    return problems, gained


def _refusal_key(reason: str) -> str:
    """The stable half of a refusal.

    The full sentence carries the searched-count and the field list and is
    meant to be read by a person; pinning it whole would make the expectations
    file churn on every wording change. This keeps the part that is a claim.
    """
    if "found no transformer stack" in reason:
        return "no transformer stack"
    return reason.strip("'").split(" -- ")[0][:80]


# --------------------------------------------------------------------------
# The image: what it really loads, against what this checkout claims.
# --------------------------------------------------------------------------


@dataclass
class Divergence:
    """Where `VLLM_ARCHITECTURES` and the pinned image disagree.

    The static table is a copy of a fact that lives somewhere else, and this
    is the check that the copy is still true. Both directions are real bugs
    and they fail differently, which is why they are counted apart.
    """

    image: str
    version: str
    agreed: int
    #: The image loads it, the table does not name it. Every one of these is a
    #: model derate refuses with a 400 that the runtime would have served --
    #: `Gemma4ForConditionalGeneration` was exactly this, for weeks.
    refused_anyway: list[str]
    #: The table names it, the image cannot load it. This is the worse one: it
    #: clears every gate and dies at load, minutes in, on the hardware.
    claimed_but_absent: list[str]

    def __bool__(self) -> bool:
        return bool(self.refused_anyway or self.claimed_but_absent)

    def render(self) -> str:
        lines = [
            f"architecture sweep: {self.agreed + len(self.refused_anyway) + len(self.claimed_but_absent)} "
            f"from {self.image} ({self.version})",
            f"  {self.agreed:>4} agree with VLLM_ARCHITECTURES",
        ]
        if self.refused_anyway:
            lines.append(
                f"  {len(self.refused_anyway):>4} the image LOADS but the table refuses "
                f"-- a 400 on a model that would have served:"
            )
            lines += [f"        {name}" for name in self.refused_anyway]
        if self.claimed_but_absent:
            lines.append(
                f"  {len(self.claimed_but_absent):>4} the table CLAIMS but the image "
                f"cannot load -- clears every gate, dies at load:"
            )
            lines += [f"        {name}" for name in self.claimed_but_absent]
        return "\n".join(lines)


def vllm_image() -> str:
    """The image the `vllm` runtime actually launches, env override included."""
    from control_plane.deploy.flags import RUNTIMES as RUNTIME_SPECS
    from control_plane.deploy.recipes import container_image

    return container_image(RUNTIME_SPECS["vllm"])


def architecture_divergence(*, cache_dir: Path | None = None) -> Divergence | None:
    """Read the image's registry and diff it against the static table.

    None when the image cannot be read here -- no docker, or it has not been
    pulled onto this machine. That is a real deployment and not a failure, so
    the caller reports it as unchecked rather than as agreement. Silence and a
    clean bill of health must not look the same.
    """
    from control_plane.resolver import imageprobe, support

    image = vllm_image()
    found = imageprobe.probe("vllm", image, cache_dir=cache_dir)
    if found is None:
        return None
    static = set(support.VLLM_ARCHITECTURES)
    live = set(found.architectures)
    return Divergence(
        image=image,
        version=found.version,
        agreed=len(static & live),
        refused_anyway=sorted(live - static),
        claimed_but_absent=sorted(static - live),
    )


# --------------------------------------------------------------------------
# The live catalogue.
# --------------------------------------------------------------------------


def origin() -> str:
    return os.environ.get("DERATE_CHECK_ORIGIN", "http://localhost:8088").rstrip("/")


def _get(url: str, timeout: float) -> dict | list:
    with request.urlopen(url, timeout=timeout) as answer:  # noqa: S310 - local origin
        return json.loads(answer.read().decode())


#: A catalogue row's `where` values that mean the weights are, or could be, on
#: this cluster. Anything else in the catalogue is a remote model.
_LOCAL_PLACES = frozenset({"ondisk", "running", "catalog", "cached", "deployed"})


def catalogue(timeout: float = 30.0, *, local_only: bool = True) -> list[str]:
    """Model ids from the coordinator, remote-only ones dropped by default.

    A row whose `where` is just `['offered']` is a provider model -- Amazon
    Nova, an OpenRouter listing -- and has no HuggingFace repo to read a shape
    out of. Asking about one produces a 401 that is entirely correct and
    entirely uninteresting, and on this box that was 27 of the first 60 models
    swept: enough noise to hide the thing the sweep exists to find.
    """
    rows = _get(f"{origin()}/api/models", timeout)
    rows = rows if isinstance(rows, list) else rows.get("models", [])
    found: dict[str, bool] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("model_id"):
            continue
        where = set(row.get("where") or ())
        remote = bool(where) and not (where & _LOCAL_PLACES)
        model_id = row["model_id"]
        found[model_id] = found.get(model_id, True) and remote
    if local_only:
        return sorted(m for m, remote in found.items() if not remote)
    return sorted(found)


def catalogue_kinds(timeout: float = 30.0) -> dict[str, bool]:
    """``{model_id: is_remote}`` for the whole catalogue.

    The pair that `--all` needs: sweeping provider models is fine as long as
    the report knows which ones they are, because "no config.json" is the
    right answer for a model served over somebody else's API and the wrong
    answer for one sitting on a disk here.
    """
    rows = _get(f"{origin()}/api/models", timeout)
    rows = rows if isinstance(rows, list) else rows.get("models", [])
    kinds: dict[str, bool] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("model_id"):
            continue
        where = set(row.get("where") or ())
        remote = bool(where) and not (where & _LOCAL_PLACES)
        model_id = row["model_id"]
        kinds[model_id] = kinds.get(model_id, True) and remote
    return kinds


def resolve_live(model_id: str, timeout: float) -> Outcome:
    """Ask the coordinator, not the hub.

    It has the shape cache, the credentials and the resolver already wired, so
    this measures the thing an operator actually experiences: what the running
    system says about a model in its own catalogue.
    """
    url = f"{origin()}/api/models/detail?" + parse.urlencode({"model_id": model_id})
    try:
        payload = _get(url, timeout)
    except error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode())
            detail = body.get("error", {}).get("message") or str(body)
        except Exception:
            detail = f"{exc.code} {exc.reason}"
        return Outcome(model_id, False, reason=f"{exc.code}: {detail}"[:300])
    except Exception as exc:
        return Outcome(model_id, False, reason=f"{type(exc).__name__}: {exc}"[:300])

    if not isinstance(payload, dict) or payload.get("error"):
        message = (payload or {}).get("error", {})
        message = message.get("message") if isinstance(message, dict) else str(message)
        return Outcome(model_id, False, reason=str(message)[:300])

    shape = payload.get("shape") or {}
    if not shape.get("num_layers"):
        return Outcome(model_id, False, reason="no shape in the answer")
    runtimes = {
        e.get("runtime"): e.get("level")
        for e in (payload.get("support") or {}).get("runtimes", [])
    }
    return Outcome(
        model_id,
        True,
        shape=(
            shape.get("num_layers", 0), shape.get("hidden_size", 0),
            shape.get("num_attention_heads", 0), shape.get("num_kv_heads", 0),
        ),
        experts=shape.get("num_experts", 0) or 0,
        architectures=tuple(payload.get("architectures") or ()),
        runtimes=runtimes,
    )


def sweep_live(
    model_ids: list[str],
    *,
    workers: int = 8,
    timeout: float = 120.0,
    kinds: dict[str, bool] | None = None,
):
    kinds = kinds or {}

    def one(model_id: str) -> Outcome:
        found = resolve_live(model_id, timeout)
        found.remote = kinds.get(model_id, False)
        return found

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, model_ids))


# --------------------------------------------------------------------------
# Reporting.
# --------------------------------------------------------------------------


def report(results: list[Outcome], *, verbose: bool = False, corpus: bool = False) -> int:
    """Print the table and return the number of models worth flagging."""
    unreadable = [r for r in results if r.unreadable]
    remote = [r for r in results if r.remote and not r.resolved]
    hub = [r for r in results if not r.resolved and r.hub_fault and not r.remote]
    resolved = [r for r in results if r.resolved]
    servable = [r for r in resolved if r.servable]

    print(f"{len(results)} models")
    print(f"  {len(resolved):>4} resolved   ({len(servable)} servable by some runtime here)")
    if remote:
        print(f"  {len(remote):>4} provider-offered, no local weights  (no shape expected)")
    if hub:
        print(f"  {len(hub):>4} not answered by the hub  (gated, missing, unreachable)")
    label = "no transformer stack found" if corpus else "UNREADABLE"
    if unreadable:
        print(f"  {len(unreadable):>4} {label}")

    if verbose and resolved:
        print("\nresolved:")
        for r in sorted(resolved, key=lambda r: r.model_id):
            moe = f"  moe={r.experts}" if r.experts else ""
            runtimes = ",".join(
                k for k, v in (r.runtimes or {}).items() if v == "supported"
            ) or "-none-"
            print(f"  {r.model_id:<52} {str(r.shape):<26}{moe:<10} [{runtimes}]")

    if hub and verbose:
        print("\nnot answered by the hub:")
        for r in sorted(hub, key=lambda r: r.model_id):
            print(f"  {r.model_id:<52} {r.reason[:90]}")

    if unreadable:
        print(f"\n{label}:")
        for r in sorted(unreadable, key=lambda r: r.model_id):
            arch = f" {r.architectures[0]}" if r.architectures else ""
            print(f"  {r.model_id}{arch}\n      {r.reason[:160]}")
    return len(unreadable)


def _default_probe_cache() -> Path:
    """Where the coordinator keeps its probes, so the sweep reuses them.

    Sharing the cache is the difference between this costing a container start
    and costing nothing: the running coordinator has already asked the image
    what it loads, and the answer is keyed by image id, so it is as valid here
    as it is there.
    """
    from control_plane.paths import data_dir

    return Path(data_dir()) / "cache" / "runtimes"


def sweep_curated(*, timeout: float = 120.0) -> int:
    """Resolve every model on the curated strip, for real, against the hub.

    The strip is the first thing on the models screen and the only thing on it
    nobody chose, so a withdrawn or renamed repository is a dead row in the
    most visible place in the product -- and until this existed, nothing
    anywhere noticed. `tests/unit/test_catalog.py` holds the list to its shape
    and its cap; only the network can answer whether the ids are still real.

    A GATED repository is reported apart and does not fail. It is a licence
    this box has not accepted, not a broken entry:
    `meta-llama/Llama-3.3-70B-Instruct` has been on this list since the
    beginning and 401s here. Counting that as a failure would make the check
    depend on whose token is in the environment, which is exactly the kind of
    result that gets ignored.
    """
    from control_plane.fit.catalog import CURATED_MODELS
    from control_plane.resolver.resolver import ModelResolver
    from control_plane.resolver.types import MetadataUnavailable, ModelNotFound

    resolver = ModelResolver()
    ok: list[str] = []
    gated: list[tuple[str, str]] = []
    broken: list[tuple[str, str]] = []

    print(f"resolving {len(CURATED_MODELS)} curated models ...\n")
    for entry in CURATED_MODELS:
        try:
            shape = resolver.resolve(entry.model_id)
        except ModelNotFound as exc:
            broken.append((entry.model_id, str(exc)))
        except MetadataUnavailable as exc:
            # 401/403 is a licence; anything else here is the hub failing or
            # the repo being unreadable, and that IS a broken row.
            text = str(exc)
            if "gated" in text or "401" in text or "403" in text:
                gated.append((entry.model_id, text))
            else:
                broken.append((entry.model_id, text))
        except Exception as exc:  # pragma: no cover - defensive
            broken.append((entry.model_id, f"{type(exc).__name__}: {exc}"))
        else:
            ok.append(entry.model_id)
            print(
                f"  ok      {entry.label:24} {shape.total_params / 1e9:7.1f}B "
                f"{shape.dtype}"
            )

    for model_id, why in gated:
        print(f"  gated   {model_id}\n            {why}")
    for model_id, why in broken:
        print(f"  BROKEN  {model_id}\n            {why}")

    print(
        f"\n{len(ok)} resolved, {len(gated)} gated here, {len(broken)} broken"
    )
    if gated and not broken:
        print("Gated is not a failure: set HF_TOKEN to check those too.")
    return 1 if broken else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true",
                        help="sweep the coordinator's catalogue instead of the fixtures")
    parser.add_argument("--limit", type=int, default=0, help="stop after N models")
    parser.add_argument("--all", action="store_true",
                        help="include provider-only models, which have no local shape")
    parser.add_argument("--curated", action="store_true",
                        help="resolve every id in fit/catalog.py CURATED_MODELS")
    parser.add_argument("--arch", action="store_true",
                        help="diff VLLM_ARCHITECTURES against the pinned image's registry")
    parser.add_argument("--probe-cache", default=None,
                        help="where image probes are cached (default: the data dir)")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--regenerate", action="store_true",
                        help="rewrite tests/resolver_data/EXPECTED.json from the fixtures")
    args = parser.parse_args(argv)

    probe_cache = Path(args.probe_cache) if args.probe_cache else _default_probe_cache()

    if args.curated:
        return sweep_curated(timeout=args.timeout)

    if args.arch:
        found = architecture_divergence(cache_dir=probe_cache)
        if found is None:
            print(f"the vllm image ({vllm_image()}) cannot be read here.")
            print("Nothing was checked -- this is not agreement.")
            return 2
        print(found.render())
        return 1 if found else 0

    if args.regenerate:
        EXPECTED.write_text(
            json.dumps(corpus_expectations(), indent=2, sort_keys=True) + "\n"
        )
        print(f"wrote {EXPECTED} ({len(corpus_names())} configs)")
        return 0

    if args.live:
        try:
            kinds = catalogue_kinds(args.timeout)
            models = sorted(kinds) if args.all else sorted(
                m for m, is_remote in kinds.items() if not is_remote
            )
        except Exception as exc:
            print(f"no coordinator on {origin()}: {type(exc).__name__}: {exc}")
            print("start one, or set DERATE_CHECK_ORIGIN.")
            return 2
        if args.limit:
            models = models[: args.limit]
        print(f"sweeping {len(models)} models from {origin()} ...\n")
        results = sweep_live(models, workers=args.workers, timeout=args.timeout,
                             kinds=kinds)
    else:
        names = corpus_names()
        if args.limit:
            names = names[: args.limit]
        results = [resolve_corpus(n) for n in names]
        report(results, verbose=args.verbose, corpus=True)

        # The same corpus down the path a real coordinator takes. Reported as
        # unchecked rather than skipped in silence, because "we could not look"
        # and "we looked and it was fine" are not the same line.
        probed = compare_probed(cache_dir=probe_cache) if not args.limit else None
        if probed is None:
            print("\ncorpus (probed image)    unchecked -- no readable vllm image here")
        else:
            problems, gained = probed
            print(f"\ncorpus (probed image)    {len(corpus_names())} configs, "
                  f"{len(problems)} problem(s), {len(gained)} model(s) the image adds")
            for line in problems:
                print(f"  {line}")
            for line in gained:
                print(f"  + {line}")
            if problems:
                return 1

        drift = compare_corpus() if not args.limit else Drift([], [])
        if drift:
            print()
            print(drift.render())
            return 1
        print(f"\nall {len(names)} match {EXPECTED.name}")
        return 0

    flagged = report(results, verbose=args.verbose)
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
