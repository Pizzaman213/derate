"""Media URLs in a /v1 chat body must name a public host.

A vision or audio model fetches ``image_url``/``video_url``/``audio_url``
itself, from the node it runs on -- which sits on the LAN, beside the node
agents, the coordinator's own ports and a cloud metadata service. Over a WAN
edge that turns any key holder into a request forger for that network
(``http://127.0.0.1:18081/agent/...``, ``http://169.254.169.254/...``). So a URL
whose host is an address that is not globally routable, or a name only a LAN
can resolve, is refused before anything is forwarded.

Standard library only. Names are NOT resolved: a public name that resolves to
a private address (DNS rebinding, ``*.nip.io``), a public URL that redirects to
a private one, and a private TLD this list does not know are not caught here,
and the rule says so rather than pretending to.

What IS caught is every spelling of an address the backend's resolver would
turn into a private one without asking DNS: ``inet_aton``'s short, hex and
octal IPv4 forms (``127.1``, ``0x7f.0.0.1``, ``0177.0.0.1``), IPv4-mapped and
NAT64-embedded IPv6, IDNA dots and fullwidth letters (``a.local。``), and an
authority carrying ``%``, ``\\`` or whitespace -- the shapes two URL parsers
disagree about, where this one would check one host and the fetcher
connect to another.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlsplit

#: Suffixes that only a local resolver answers for.
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")
#: The characters ``inet_aton`` reads as an IPv4 address: digits, hex, dots.
_LEGACY_IPV4 = re.compile(r"[0-9a-fx.]+")
#: RFC 6052's well-known NAT64 prefix: the last 32 bits are an IPv4 address.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
#: Longer than any browser or fetcher accepts as a link in practice; bounds
#: urlsplit's cache and every per-character pass below.
_MAX_URL_CHARS = 8192
#: RFC 1035: a name is at most 253 characters, a label at most 63. Checked
#: before IDNA, whose nameprep is super-linear in the input.
_MAX_HOST_CHARS = 253
_MAX_LABEL_CHARS = 63
#: The dots IDNA treats as label separators (UTS 46).
_LABEL_DOTS = re.compile("[.\u3002\uff0e\uff61]")
_DIGIT = re.compile(r"[0-9]")
#: An authority carrying these is one two parsers may read differently.
_UNPLAIN_NETLOC = re.compile(r"[%\\\x00-\x20\x7f]")


def _urls_of(part: dict):
    """Every media URL a content part names: OpenAI's chat shape, vLLM's plain
    string forms (``{"video_url": "..."}``) and the Responses shape (``type:
    input_image``). A non-string is skipped: the backend answers a malformed
    part better than this does."""
    for field in ("image_url", "video_url", "audio_url"):
        value = part.get(field)
        if isinstance(value, dict):
            yield value.get("url")
        elif isinstance(value, str):
            yield value
    if part.get("type") == "input_image":
        value = part.get("input_image")
        yield value.get("url") if isinstance(value, dict) else value


def _address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """*host* as the address a resolver would connect to without asking DNS,
    or None when it is a name."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not _LEGACY_IPV4.fullmatch(host) or not _DIGIT.search(host):
            return None
        try:
            return ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        if address in _NAT64:
            return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address


def _refusal(url: str) -> str | None:
    if url[:5].lower() == "data:":
        # Before any parse: urlsplit's lru_cache would pin a multi-MiB payload.
        return None
    if len(url) > _MAX_URL_CHARS:
        return (
            f"Media URLs must be at most {_MAX_URL_CHARS} characters; "
            f"{_echo(url)} is {len(url)}."
        )
    try:
        parts = urlsplit(url)
    except ValueError:
        # "http://[x/", "http://[127.0.0.1]/": urlsplit raises on its own.
        return f"Media URLs must be valid URLs; {_echo(url)} is not one."
    scheme = parts.scheme.lower()
    if scheme == "data":
        return None
    if scheme not in ("http", "https"):
        return f"Media URLs must be http(s) or data:; got scheme {_echo(scheme)}."
    if _UNPLAIN_NETLOC.search(parts.netloc):
        return f"Media URLs must name their host plainly; {_echo(parts.netloc)} does not."
    try:
        host = parts.hostname
    except ValueError:
        host = None
    if not host:
        return f"Media URLs must point at a public host; {_echo(url)} names none."
    host = host.rstrip(".")
    if len(host) > _MAX_HOST_CHARS or any(
        len(label) > _MAX_LABEL_CHARS for label in _LABEL_DOTS.split(host)
    ):
        return f"Media URLs must point at a public host; {_echo(host)} is not a valid name."
    if not host.isascii():
        # The fetcher IDNA-encodes a non-ASCII name, which folds fullwidth
        # letters and the ideographic full stop into ASCII. Judge that name.
        try:
            host = host.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError:
            return f"Media URLs must point at a public host; {_echo(host)} is not a valid name."
    address = _address(host)
    if address is not None:
        if address.is_global and not address.is_multicast:
            return None
        return f"Media URLs must point at a public host; {_echo(host)} is not one."
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES) or "." not in host:
        return f"Media URLs must point at a public host; {_echo(host)} is not one."
    return None


def _echo(value: str) -> str:
    """*value* as a refusal quotes it: repr'd, and cut at 200 characters --
    a 25 MiB URL must not come back as a 25 MiB error body."""
    return repr(value if len(value) <= 200 else value[:200] + "...")


def media_url_refusal(body: dict) -> str | None:
    """The first media URL in a chat body that names a non-public host, as the
    message to refuse with, or None. data: URIs always pass. Does not resolve
    names, so a public name that resolves privately (DNS rebinding) is not
    caught here."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return None
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            for url in _urls_of(part):
                if isinstance(url, str):
                    why = _refusal(url)
                    if why is not None:
                        return why
    return None
