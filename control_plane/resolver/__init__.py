"""Model resolver: a HuggingFace model id in, a complete ``ModelShape`` out.

    from control_plane.resolver import ModelResolver

    resolver = ModelResolver()
    shape = resolver.resolve("openai/gpt-oss-120b")        # the contract type
    full = resolver.resolve_full("openai/gpt-oss-120b")     # plus provenance

``resolve()`` is the ``ResolverPort`` method everything downstream calls.
``resolve_full()`` adds the warnings, the runtime support verdict, and the real
on-disk weight bytes, which is what a human should see before a launch.
"""

from control_plane.contracts.quant import BYTES_PER_PARAM, bytes_per_param, normalize_dtype

from .cache import ShapeCache
from .gguf import GGUFHeader, read_gguf_file
from .hf import HubClient
from .params import ParamBreakdown, analytic_breakdown
from .resolver import ModelResolver
from .stub import StubResolver
from .support import check_nodes, quant_requirement
from .types import (
    MetadataUnavailable,
    ModelNotFound,
    ParamSource,
    QuantRequirement,
    QuantSource,
    QuantVariant,
    Resolution,
    ResolverError,
    RuntimeSupport,
    SupportLevel,
    SupportVerdict,
    UnsupportedArchitecture,
)

__all__ = [
    "BYTES_PER_PARAM",
    "GGUFHeader",
    "HubClient",
    "MetadataUnavailable",
    "ModelNotFound",
    "ModelResolver",
    "ParamBreakdown",
    "ParamSource",
    "QuantRequirement",
    "QuantSource",
    "QuantVariant",
    "Resolution",
    "ResolverError",
    "RuntimeSupport",
    "ShapeCache",
    "StubResolver",
    "SupportLevel",
    "SupportVerdict",
    "UnsupportedArchitecture",
    "analytic_breakdown",
    "bytes_per_param",
    "check_nodes",
    "normalize_dtype",
    "quant_requirement",
    "read_gguf_file",
]
