"""The few form fields the gateway reads out of a multipart upload.

``/v1/audio/transcriptions`` is forwarded byte for byte, so this is deliberately
not a general multipart parser: it finds one text field (the model, the answer
format, ``stream``) and its byte span, so the model can be swapped without
re-encoding the file beside it. Every refusal here exists because a gateway
that reads one value while the upstream's parser reads another would authorise
and price the wrong model.
"""

from __future__ import annotations

import re

#: Parts one multipart upload may carry: a file and a handful of fields.
#: `_field_span` walks them in Python, once per field it reads.
MAX_FORM_PARTS = 64


class AmbiguousBoundary(ValueError):
    """The content-type names more than one ``boundary`` parameter.

    Parsers disagree on which one wins (first here, last in most upstreams),
    so the part the gateway authorises and the part the provider reads could
    differ. Refused rather than guessed.
    """


class DuplicateField(ValueError):
    """A form field the gateway reads appears in more than one part."""


class TooManyParts(ValueError):
    """An upload with more than `MAX_FORM_PARTS` parts: refused rather than
    walked, since every field read walks them all."""


class MalformedPart(ValueError):
    """A body another multipart parser could split or name differently: a lone
    CR or LF, an obs-fold or an RFC 2231 ``name*=`` in a part's headers, or a
    delimiter out of place (see :func:`_field_span`)."""


#: Quoted or bare, any case: upstream parsers accept ``name=model`` and
#: ``NAME="model"`` both, so a second spelling must count as a second part.
_DISPOSITION_NAME = re.compile(r';\s*name\s*=\s*"?([^";]*)"?', re.IGNORECASE)
#: Spellings this parser does not read as a name and a lenient upstream (Go's
#: textproto) does: a header continued on a folded line, and ``name*=``.
#: RFC 7578 forbids both, so a part carrying either is refused.
_OBS_FOLD = re.compile(rb"\r\n[ \t]")
_RFC2231_NAME = re.compile(rb";\s*name\*", re.IGNORECASE)


#: RFC 2046's ``bchars``: what a boundary may contain, at most 70 of them, not
#: ending in a space. A quote, a backslash or a ``;`` inside one is a parser
#: differential, not a boundary.
_BOUNDARY = re.compile(r"[0-9A-Za-z'()+_,\-./:=? ]{0,69}[0-9A-Za-z'()+_,\-./:=?]")


def boundary_of(content_type: str) -> bytes | None:
    """The boundary token out of a multipart content-type header.

    :class:`AmbiguousBoundary` when the header names more than one, names one
    in RFC 2231 form (``boundary*=``, ``boundary*0=``: Python's and Go's
    parsers let it override the plain one, which this one would read), or
    names one that is not RFC 2046 ``bchars`` (a quoted-pair escape or a ``;``
    inside quotes reads differently once unescaped).
    """
    found: bytes | None = None
    seen = False
    for part in content_type.split(";")[1:]:
        raw_key, _, value = part.strip().partition("=")
        key = raw_key.strip().lower()
        if key.startswith("boundary*"):
            raise AmbiguousBoundary(content_type)
        if key == "boundary":
            if seen or raw_key != raw_key.rstrip() or value != value.strip():
                # Whitespace around `=` is stripped by Python's parser and kept
                # by WHATWG's (Node): two boundaries for one header.
                raise AmbiguousBoundary(content_type)
            seen = True
            token = value
            if len(token) >= 2 and token[0] == token[-1] == '"':
                token = token[1:-1]
            if token and not _BOUNDARY.fullmatch(token):
                raise AmbiguousBoundary(content_type)
            found = token.encode("latin-1") if token else None
    return found


def _part_name(headers: bytes) -> str | None:
    """The ``name`` parameter of a part's content-disposition, or None.

    Only that header and only that parameter: ``filename="model"`` or a
    ``name="model"`` in some other header is not the field. A part with two
    content-disposition headers, or two ``name=`` parameters, is refused: the
    upstream parser keeps the last where this one would read the first. A
    header block a lenient parser would read a name out of where this one
    reads none is refused too (:class:`MalformedPart`).
    """
    lone = headers.replace(b"\r\n", b"")  # a lone CR is a line break to Werkzeug, a lone LF to Go
    if b"\n" in lone or b"\r" in lone or _OBS_FOLD.search(headers) or _RFC2231_NAME.search(headers):
        raise MalformedPart()
    found: str | None = None
    seen = False
    for line in headers.split(b"\r\n"):
        key, sep, value = line.partition(b":")
        if not sep or key.strip().lower() != b"content-disposition":
            continue
        if seen:
            raise DuplicateField("content-disposition")
        seen = True
        names = _DISPOSITION_NAME.findall(value.decode("latin-1"))
        if len(names) > 1:
            raise DuplicateField("content-disposition")
        found = names[0].strip() if names else None
    return found


def _field_span(raw: bytes, content_type: str, name: str) -> tuple[str, int, int] | None:
    """Find a simple form field: its value, and where the value sits in *raw*.

    Anything it does not understand it declines to find, and the caller
    answers with a missing-parameter 400 rather than guessing. A second part
    with the same name raises :class:`DuplicateField`: the gateway would
    authorise and price one value while the upstream's parser takes the other.

    Every other way two parsers could split the body differently raises
    :class:`MalformedPart`: a part that never ends (no blank line after its
    headers, no delimiter after its value), a delimiter not at the start of a
    line or not followed by CRLF or ``--``, a field value that is not UTF-8,
    and the boundary appearing anywhere the walk did not accept it as a
    delimiter (RFC 2046: it MUST NOT appear inside a part). Python's parser
    reads ``--bX`` as data and Go's reads ``--b`` at the start of a value as a
    delimiter; either would see a part this walk did not.
    """
    boundary = boundary_of(content_type)
    if not boundary:
        return None
    delimiter = b"--" + boundary
    found: tuple[str, int, int] | None = None
    start = raw.find(delimiter)
    if start < 0:
        return None
    if start and raw[start - 2:start] != b"\r\n":
        raise MalformedPart()
    for walked in range(1, MAX_FORM_PARTS + 2):
        cursor = start + len(delimiter)
        if raw.startswith(b"--", cursor):  # the closing delimiter
            if raw.count(delimiter) != walked:
                raise MalformedPart()
            return found
        if not raw.startswith(b"\r\n", cursor):
            raise MalformedPart()
        head_end = raw.find(b"\r\n\r\n", cursor)
        body_end = raw.find(b"\r\n" + delimiter, head_end + 4) if head_end >= 0 else -1
        if body_end < 0:
            raise MalformedPart()
        headers = raw[cursor:head_end]
        body_start = head_end + 4
        part_name = _part_name(headers)
        if part_name is not None and part_name.lower() == name.lower():
            if found is not None:
                raise DuplicateField(name)
            try:
                found = raw[body_start:body_end].decode("utf-8"), body_start, body_end
            except UnicodeDecodeError:
                raise MalformedPart() from None
        start = body_end + 2
    raise TooManyParts(MAX_FORM_PARTS)


def read_field(raw: bytes, content_type: str, name: str) -> str | None:
    """The text value of form field *name*, stripped, or None."""
    found = _field_span(raw, content_type, name)
    return found[0].strip() if found else None


def rewrite_field(raw: bytes, content_type: str, name: str, value: str) -> bytes | None:
    """Swap one field's value, leaving every other byte alone.

    Needed because a remote provider calls the model something else, and on
    this path the name is inside the body rather than in a dict we can copy.
    """
    found = _field_span(raw, content_type, name)
    if found is None:
        return None
    _, start, end = found
    return raw[:start] + value.encode("utf-8") + raw[end:]
