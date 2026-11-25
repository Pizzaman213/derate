"""What a ``.gguf`` filename tells you, and what it does not.

A GGUF repository is a directory of files, and only some of them are weights.
The rest -- vision projectors, calibration matrices, speculative-decoding draft
heads, big-endian rebuilds -- sit in the same listing under the same extension,
and a variant list that takes the extension at its word offers a 600 MB
projector as if it were a model you could run.

The rules here are facts about how the HuggingFace GGUF ecosystem names things,
gathered by reading what publishers actually ship. They are deliberately
conservative in one direction: a file we cannot classify is a weight file. A
false exclusion hides a quantization that exists, which is the failure this
module was written to fix; a false inclusion shows one extra row with a
measured size beside it, which a person can see and dismiss.

Kept free of network and of the resolver so it can be tested against a list of
strings.
"""

from __future__ import annotations

import re

#: Shard suffix llama.cpp writes when a quantization spans several files.
#: ``-00001-of-00002`` immediately before the extension.
_SHARD_RE = re.compile(r"-(\d{5})-of-(\d{5})(?=\.gguf$)", re.IGNORECASE)

#: Split the basename the way publishers punctuate it. Every rule below is
#: expressed over these tokens rather than over the raw string, because
#: substring tests are how "be" matches "bert" and "draft" matches nothing you
#: meant.
_TOKEN_SPLIT = re.compile(r"[-_.\s]+")

#: Files that are never weights, matched anywhere in the name. "mmproj" is a
#: llama.cpp coinage for a multimodal projector and appears in no quantization
#: scheme; "mtp"/"dflash"/"dspark"/"draft" name speculative-decoding heads,
#: which are their own small model and not a build of this one.
#: ``unsloth/Inkling-2-Mini-GGUF`` ships ``dspark/dspark-…-BF16.gguf`` at
#: 0.6 GiB, and before "dspark" was here it was recommended as the model's BF16.
_NEVER_WEIGHTS = frozenset({"mmproj", "mtp", "dflash", "dspark", "draft", "drafter"})

#: Endianness rebuilds. Only ever the final token, and "be" is too short to
#: test anywhere else -- it is a substring of half the vocabulary.
_BIG_ENDIAN_TAIL = frozenset({"be", "bigendian"})


def tokens(filename: str) -> list[str]:
    """The basename, lowercased, split on publisher punctuation, ``.gguf`` gone."""
    base = filename.rsplit("/", 1)[-1].lower()
    if base.endswith(".gguf"):
        base = base[: -len(".gguf")]
    return [t for t in _TOKEN_SPLIT.split(base) if t]


def is_weight_file(filename: str) -> bool:
    """Is this ``.gguf`` a set of weights, or something else in the same folder?

    Four exclusions, each a real file that currently reaches the variant list:

    ``mmproj-*``     a vision projector, loaded *beside* a model, never instead
                     of one.
    ``imatrix``      an importance matrix -- calibration data used to build the
                     IQ quants, not a thing you serve. Matched as the leading
                     token, or anywhere in a name that carries no quantization
                     token (bartowski's ``<org>_<model>-imatrix.gguf``, 0.1 GiB
                     beside a 48.6 GiB BF16). Never by substring alone, because
                     ``Model-IQ4_XS-imatrix.gguf`` is a real quantization
                     advertising how it was made, and that test would throw
                     away the entire IQ ladder of every repository that labels
                     it.
    ``*-mtp``,       a multi-token-prediction, DSpark or draft head.
    ``dspark-*``
    ``*-be``         a big-endian rebuild, unloadable on every machine here.

    AppleDouble sidecars (``._name.gguf``) are excluded too: a macOS publisher
    uploading from Finder ships one per real file, so they arrive as an exact
    duplicate listing at a few KiB each.
    """
    base = filename.rsplit("/", 1)[-1]
    if base.startswith("._"):
        return False

    parts = tokens(filename)
    if not parts:
        return False
    if parts[0] == "imatrix":
        return False
    if "imatrix" in parts and quant_token(filename) is None:
        return False
    if _NEVER_WEIGHTS.intersection(parts):
        return False
    if parts[-1] in _BIG_ENDIAN_TAIL:
        return False
    return True


def is_mmproj(filename: str) -> bool:
    """A vision projector. Excluded from variants, but worth naming separately:
    its presence is the honest signal that a repo's model is multimodal."""
    return "mmproj" in tokens(filename)


def shard_family(filename: str) -> tuple[str, int, int] | None:
    """``(stem without the shard suffix, this index, the total)``, or ``None``.

    ``None`` means a single-file quantization, which is the common case and not
    an error.
    """
    match = _SHARD_RE.search(filename)
    if match is None:
        return None
    stem = filename[: match.start()] + filename[match.end():]
    return stem, int(match.group(1)), int(match.group(2))


def shard_name(filename: str, index: int) -> str:
    """The name of shard *index* of the family *filename* belongs to.

    Rebuilt from the same match `shard_family` reads, so ``x-00001-of-00003``
    becomes ``x-00002-of-00003`` with everything else -- a quant subfolder, the
    publisher's case -- untouched. *filename* must be a shard.
    """
    match = _SHARD_RE.search(filename)
    if match is None:
        raise ValueError(f"{filename!r} is not a shard")
    total = int(match.group(2))
    return f"{filename[: match.start()]}-{index:05d}-of-{total:05d}{filename[match.end():]}"



def variant_stem(filename: str) -> str:
    """The filename a shard family shares. The grouping key for a variant.

    Two files differing only by shard index are one download; two files
    differing anywhere else are two variants, including when they differ only
    by a bits-per-weight suffix. ``IQ4_XS-3.53bpw`` and ``IQ4_XS-4.19bpw`` are
    the same scheme at two sizes, and collapsing them would hide one build
    behind the other's size.
    """
    family = shard_family(filename)
    return family[0] if family else filename


#: The published quantization token, most specific first. This is what the
#: publisher called it, not what we price it as -- ``quant_detect.from_name``
#: answers the second question and its answer for ``UD-Q4_K_XL`` is ``q4_k_m``.
#: Rendering that instead would paraphrase somebody else's name.
_TOKEN_RE = re.compile(
    r"(?P<token>"
    r"(?:UD-)?"
    r"(?:"
    r"MXFP\d+(?:_[A-Z0-9]+)*"
    r"|NVFP\d+(?:_[A-Z0-9]+)*"
    r"|IQ\d+_[A-Z]+(?:_[A-Z]+)?"
    r"|TQ\d+_\d+"
    r"|Q\d+_K_[A-Z]+"
    r"|Q\d+_K"
    r"|Q\d+_\d+"
    r"|BF16|F16|F32|FP16|FP32"
    r")"
    r")",
    re.IGNORECASE,
)

#: A bits-per-weight suffix some publishers append to disambiguate two builds
#: of one scheme: ``IQ4_XS-3.53bpw``.
_BPW_RE = re.compile(r"(\d+\.\d+)\s*bpw", re.IGNORECASE)


def quant_token(filename: str) -> str | None:
    """The scheme as the publisher wrote it, ``None`` when the name does not say.

    Case is preserved from the file: publishers write ``UD-Q4_K_XL`` and that
    is the string a person recognises. The bits-per-weight suffix is carried
    when present, because it is the only thing telling two builds apart.
    """
    base = _bare_stem(filename)

    match = None
    for match in _TOKEN_RE.finditer(base):
        pass  # the last match: "Qwen3-30B-A3B-UD-Q4_K_XL" -- not the "3" in A3B
    if match is None:
        return None

    token = match.group("token")
    bpw = _BPW_RE.search(base)
    return f"{token}-{bpw.group(1)}bpw" if bpw else token


def _bare_stem(filename: str) -> str:
    """The basename with ``.gguf`` and any shard suffix removed, case kept."""
    base = filename.rsplit("/", 1)[-1]
    if base.lower().endswith(".gguf"):
        base = base[: -len(".gguf")]
    return _SHARD_RE.sub("", base + ".gguf")[: -len(".gguf")]


#: Publisher punctuation between name segments, CAPTURED so a rejoin keeps it.
#: ``_`` is not a separator here: it lives inside quant tokens (``IQ2_XXS``).
_SEGMENT_SPLIT = re.compile(r"([-.\s]+)")


def distinct_labels(labels: list[str], filenames: list[str]) -> list[str]:
    """*labels* with every duplicate replaced by what tells its files apart.

    `quant_token` drops a publisher's qualifiers, so one repository can yield
    ``IQ2_XXS`` twice (``…-AD-IQ2_XXS-Q2`` and ``…-AD-IQ2_XXS``) or ``NVFP4``
    three times (``-Lite``, ``-XL``, plain), and a ``(repo, label)`` key then
    points at several rows. A duplicated label becomes its file's stem from the
    first segment the group does not share; a label still duplicated after that
    (one basename in two folders) becomes the folder-qualified stem. A label
    that was already unique is returned untouched. The lists are parallel.
    """
    out = list(labels)
    groups: dict[str, list[int]] = {}
    for i, label in enumerate(labels):
        groups.setdefault(label, []).append(i)
    for members in groups.values():
        if len(members) < 2:
            continue
        split = [_SEGMENT_SPLIT.split(_bare_stem(filenames[i])) for i in members]
        segs = [parts[0::2] for parts in split]
        common = 0
        while all(len(s) > common + 1 for s in segs) and len({s[common] for s in segs}) == 1:
            common += 1
        for i, parts in zip(members, split):
            out[i] = "".join(parts[2 * common:])

    counts: dict[str, int] = {}
    for label in out:
        counts[label] = counts.get(label, 0) + 1
    for i, label in enumerate(out):
        if counts[label] > 1:
            stem = variant_stem(filenames[i])
            out[i] = stem[: -len(".gguf")] if stem.lower().endswith(".gguf") else stem
    return out


def is_hub_gguf_blob(model_id: str) -> bool:
    """Whether this is derate's own post-resolve spelling for a hub GGUF blob.

    ``hf://owner/repo/file.gguf`` is what an operator passes and what the
    resolver is documented to take. It is NOT what comes back:
    ``ModelResolver.resolve_gguf_full`` sets ``name = f"{repo}/{filename}"``
    and drops the scheme, so every consumer downstream of a resolve sees
    ``owner/repo/file.gguf``.

    That mattered because two functions in ``deploy/flags.py`` were written
    against the scheme and are both called with the resolved id --
    `local_gguf_refusal` from `manager.launch` and `llamacpp_model_spec` from
    `recipes.synthesize`. One refused every legitimate hub GGUF as a local
    path; the other declined to translate it and left sparkrun an id it cannot
    parse. Same root cause, so one predicate rather than two copies of the
    same `startswith`.

    The test is deliberately narrow, because the thing it must not swallow is
    a genuine local path -- which also ends in ``.gguf`` and also has slashes.
    A hub reference has at least three segments (owner, repo, and a filename
    that may sit under a quant subdirectory) and does not begin with a path
    sigil. ``/home/me/x.gguf`` and ``./x.gguf`` are therefore not hub blobs
    and stay refusable; ``owner/repo-GGUF`` has two segments and no ``.gguf``
    suffix, so sparkrun's own spelling is untouched.
    """
    name = model_id.strip()
    if name.startswith(("/", "~", ".")):
        return False
    if not name.lower().endswith(".gguf"):
        return False
    return len(name.split("/")) >= 3
