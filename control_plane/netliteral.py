"""Host strings the resolver reads as addresses and ``ipaddress`` does not.

Standard library only, outside both ``registry/`` and ``providers/``: the
provider ``base_url`` screen and the join ``agent_url`` screen need the same
answer, and neither package may import the other (``registry`` pulls zeroconf
and the agent; the worker path may not import ``providers``).
"""

from __future__ import annotations

import ipaddress
import re

#: A host made only of decimal/hex/octal numbers -- ``2852039166``,
#: ``0xa9fea9fe``, ``0251.0376.0251.0376`` -- that ``ipaddress`` refuses but
#: glibc's ``inet_aton`` resolves to an address (all three are 169.254.169.254).
_NUMERIC_HOST = re.compile(r"(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+)){0,3}", re.I)


def is_numeric_host_literal(host: str) -> bool:
    """A host the resolver reads as an address that ``ipaddress`` does not:
    screened as a hostname it would dodge every address check."""
    try:
        ipaddress.ip_address(host.strip("[]"))
        return False
    except ValueError:
        return bool(_NUMERIC_HOST.fullmatch(host.rstrip(".")))
