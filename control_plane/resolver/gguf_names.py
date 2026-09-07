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
#: scheme; "mtp"/"dflash"/"draft" name speculative-decoding heads, which are
#: their own small model and not a build of this one.
_NEVER_WEIGHTS = frozenset({"mmproj", "mtp", "dflash", "draft", "drafter"})

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
                     IQ quants, not a thing you serve. Matched only as the
                     leading token, because ``Model-IQ4_XS-imatrix.gguf`` is a
                     real quantization advertising how it was made, and a
                     substring test would throw away the entire IQ ladder of
                     every repository that labels it.
    ``*-mtp``        a multi-token-prediction or draft head.
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
    base = filename.rsplit("/", 1)[-1]
    if base.lower().endswith(".gguf"):
        base = base[: -len(".gguf")]
    base = _SHARD_RE.sub("", base + ".gguf")[: -len(".gguf")]

    match = None
    for match in _TOKEN_RE.finditer(base):
        pass  # the last match: "Qwen3-30B-A3B-UD-Q4_K_XL" -- not the "3" in A3B
    if match is None:
        return None

    token = match.group("token")
    bpw = _BPW_RE.search(base)
    return f"{token}-{bpw.group(1)}bpw" if bpw else token
