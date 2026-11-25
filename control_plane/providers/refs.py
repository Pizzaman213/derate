"""Which secret reference a provider may hold.

A provider's key is a NAME, resolved at request time: either the one derate
mints when a key is pasted (``DERATE_<ID>_API_KEY``, stored in secrets.json)
or an operator's own ``DERATE_SECRET_<NAME>``. Every other name -- the cluster
token, the HF token, another provider's minted key -- would be sent as a
bearer to whatever ``base_url`` the same admin chose, so it is refused.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .secrets import MINTED_REF_RE, OPERATOR_REF_RE, looks_like_secret

#: The shape of a reference derate mints for itself when a key is pasted rather
#: than named. The prefix marks the secrets.json entry as ours, which is the
#: only reason removing a provider can safely delete it: an operator's own name
#: may be shared with something else and is never touched.
MINTED_REF_PREFIX = "DERATE_"
MINTED_REF_SUFFIX = "_API_KEY"
#: The UI reads a reference as a *name* only up to this length and renders
#: anything longer as key material (``ui/src/api/redact.ts``). A minted name
#: that overflowed would display as ``***`` -- derate's own reference, hidden
#: from the operator as though it had leaked.
_MAX_REF_LEN = 64
_REF_UNSAFE = re.compile(r"[^A-Za-z0-9]+")

#: The refusal for a key pasted into the field that takes a name. One string
#: rather than three copies kept word-for-word by hand: add, update and the
#: day-0 stub all screen the same field for the same mistake, and a refusal
#: that reads differently in each reads as three different rules.
KEY_IN_REF_FIELD = (
    "api_key_ref must be the NAME of an environment variable or "
    "secrets.json key, not the key itself -- send the key as "
    '"api_key" and it will be stored in secrets.json under a '
    "reference for you"
)

REF_NOT_OURS = (
    "api_key_ref must be a derate-minted reference or DERATE_SECRET_*: "
    'send the key as "api_key" and derate stores it under this provider\'s '
    "own reference, or export it as DERATE_SECRET_<NAME> and name that"
)

#: A provider's own minted name is not taken up by naming it in a patch that
#: moves base_url: the key stored under it was pasted for the old host
#: (finding K3), and a provider that moved has to be given it again.
KEY_NOT_CARRIED = (
    "base_url moved: this provider's own key reference may hold a key pasted for the "
    "old host, and is not carried to the new one by naming it -- paste it again as api_key"
)


def minted_ref(provider_id: str) -> str:
    """The secrets.json name a pasted key is stored under for this provider.

    Uppercase throughout, which is also what keeps it clear of
    ``looks_like_secret``: that predicate wants a long run containing both
    cases and a digit, and a name shaped like this has no lowercase at all.
    """
    stem = _REF_UNSAFE.sub("_", provider_id).strip("_").upper() or "PROVIDER"
    budget = _MAX_REF_LEN - len(MINTED_REF_PREFIX) - len(MINTED_REF_SUFFIX)
    return f"{MINTED_REF_PREFIX}{stem[:budget]}{MINTED_REF_SUFFIX}"


def ref_allowed(ref: str, provider_id: str) -> bool:
    """Whether *provider_id* may hold the reference *ref*.

    Exactly its own minted name, or an operator's ``DERATE_SECRET_*``. Any
    other environment variable -- the cluster token, the HF token, another
    provider's minted key -- would be sent as a bearer to this provider's
    ``base_url``, which the same admin chose.
    """
    if ref == minted_ref(provider_id):
        return True
    return bool(OPERATOR_REF_RE.fullmatch(ref)) and not MINTED_REF_RE.fullmatch(ref)


def ref_holder(provider_ids: Iterable[str], provider_id: str) -> str | None:
    """The id among *provider_ids* that already mints *provider_id*'s name.

    Ids that differ only in case or punctuation (``openai-1``, ``openai_1``)
    mint one name, and the second would resolve -- and could carry to its own
    host -- the first's key.
    """
    own = minted_ref(provider_id)
    return next((p for p in provider_ids if minted_ref(p) == own), None)


def add_refusal(provider_ids: Iterable[str], provider_id: str, ref: str) -> str | None:
    """Why a new provider *provider_id* may not be added holding *ref*, or None.
    The real service and the day-0 stub both ask this, so the add form learns
    the same rule from either."""
    ids = list(provider_ids)
    if provider_id in ids:
        return f"provider {provider_id!r} already exists"
    clash = ref_holder(ids, provider_id)
    if clash is not None:
        return (
            f"provider {provider_id!r} would share its key reference {minted_ref(provider_id)} "
            f"with provider {clash!r}; pick an id that differs in more than case or punctuation"
        )
    if ref and not ref_allowed(ref, provider_id):
        return REF_NOT_OURS
    return None


def patch_refusal(provider_id: str, moved: bool, patch: dict) -> str | None:
    """Why *patch* may not set this provider's ``api_key_ref``, or None.

    *moved* is whether the same patch moves ``base_url``: naming its own minted
    name then, without pasting a key, would carry the key pasted for the old
    host to the new one (finding K3). Naming it without a move is the caller's
    to make safe -- ``ProviderService.update`` drops whatever secrets.json still
    held under it, so only the environment can answer for it.
    """
    if "api_key_ref" not in patch:
        return None
    ref = str(patch["api_key_ref"]).strip()
    if looks_like_secret(ref):
        return KEY_IN_REF_FIELD
    if ref and not ref_allowed(ref, provider_id):
        return REF_NOT_OURS
    pasted = bool(str(patch.get("api_key") or "").strip())
    if ref == minted_ref(provider_id) and not pasted and moved:
        return KEY_NOT_CARRIED
    return None
