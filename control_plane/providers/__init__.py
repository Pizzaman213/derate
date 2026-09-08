"""Remote OpenAI-compatible upstreams as first-class route targets.

The cluster is the default and a paid API is the overflow valve. Someone who
owns two Sparks serves from them until they saturate and then spills, without
their client knowing anything changed.

Entry points:

- :class:`ProviderService` implements ``ProviderPort`` plus ``forward``,
  ``route_targets`` and ``spend_today``.
- :func:`build_stub_service` is the day 0 fake, for wiring LOCAL_FIRST before
  any real key exists.

Keys are never logged, serialized, or echoed, and no record carries one.
``api_key_ref`` is the *name* of an environment variable or of a key in
``secrets.json`` at mode 0600, and values are resolved at request time and
nowhere else.

A key can be *given*, as ``api_key`` on an add or update spec, because
otherwise configuring a provider meant setting an environment variable on the
coordinator's host and restarting it. It is a request field and nothing more:
it is written to ``secrets.json`` and what lands on the record is the name.
"""

from .config import REDACTED
from .discovery import parse_models
from .errors import (
    AdapterUnsupportedError,
    MissingKeyError,
    ProviderError,
    ProviderNotAdmittingError,
    UnknownProviderError,
    UpstreamError,
)
from .kinds import KindSpec, known_kinds, spec_for
from .runtime import ProviderRuntime
from .secrets import Redactor, SecretRedactingFilter, SecretStore, looks_like_secret
from .serialization import (
    assert_no_key_material,
    kinds_public,
    model_public_dict,
    provider_public_dict,
)
from .service import ProviderService, UpstreamResponse
from .stub import build_stub_service, stub_transport
from .store import ProviderStore

__all__ = [
    "AdapterUnsupportedError",
    "KindSpec",
    "MissingKeyError",
    "ProviderError",
    "ProviderNotAdmittingError",
    "ProviderRuntime",
    "ProviderService",
    "ProviderStore",
    "REDACTED",
    "Redactor",
    "SecretRedactingFilter",
    "SecretStore",
    "UnknownProviderError",
    "UpstreamError",
    "UpstreamResponse",
    "assert_no_key_material",
    "build_stub_service",
    "kinds_public",
    "known_kinds",
    "looks_like_secret",
    "model_public_dict",
    "parse_models",
    "provider_public_dict",
    "spec_for",
    "stub_transport",
]
