"""`gateway/multipart.py`: the gateway and the upstream read the same fields.

The transcription upload is forwarded byte for byte, so every shape another
parser (python-multipart, Go's mime/multipart, Node's WHATWG `formData()`)
would split differently is refused: otherwise the gateway authorises and prices
one `model` while the upstream serves another.
"""

from __future__ import annotations

import pytest

from control_plane.gateway.multipart import read_field
from tests.unit.test_gateway import _multipart
from tests.unit.test_proxy_provider_hardening import _post_multipart


@pytest.mark.parametrize(
    "params",
    ["boundary=gw; boundary*=utf-8''up", "boundary=gw; boundary*0=up", 'boundary="u\\"p"', 'boundary="a;b"',
     "boundary= gw", 'boundary= "gw"', "boundary =gw"],
    ids=["rfc2231", "rfc2231-continuation", "quoted-pair", "semicolon-in-quotes",
         "space-after-equals", "space-before-quote", "space-before-equals"],
)
def test_a_boundary_another_parser_reads_differently_is_refused(params):
    """Python's and Go's parsers let ``boundary*`` override ``boundary`` and
    unescape quoted pairs; the gateway read the plain token, so it authorised
    a hidden part while the upstream served the real one."""
    body, _ = _multipart(('name="model"', b"whisper"))
    reply = _post_multipart(body, f"multipart/form-data; {params}")
    assert reply.status_code == 400, reply.text
    assert reply.json()["error"]["message"] == "ambiguous multipart boundary"


@pytest.mark.parametrize(
    "headers",
    [
        b'X-Foo: a\nContent-Disposition: form-data; name="model"',
        b'Content-Disposition: form-data;\r\n name="model"',
        b"Content-Disposition: form-data; name*=utf-8''model",
        b'X-Foo: a\rContent-Disposition: form-data; name="model"',
    ],
    ids=["bare-lf", "obs-fold", "rfc2231", "bare-cr"],
)
def test_a_part_header_a_lenient_parser_reads_differently_is_refused(headers):
    """Unnamed to the gateway, ``model`` to Go's textproto: put first, the
    gateway priced and authorised the second part's model while such an
    upstream served this one's."""
    body = (
        b"--b\r\n" + headers + b"\r\n\r\ngpt-4o-transcribe\r\n"
        b'--b\r\nContent-Disposition: form-data; name="model"\r\n\r\nwhisper-cheap\r\n'
        b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.mp3"\r\n\r\n\x00\x01\r\n'
        b"--b--\r\n"
    )
    reply = _post_multipart(body, "multipart/form-data; boundary=b")
    assert reply.status_code == 400, reply.text
    error = reply.json()["error"]
    assert error["code"] == "invalid_request"
    assert "RFC 7578" in error["message"]

_MODEL_A = b'--b\r\nContent-Disposition: form-data; name="model"\r\n\r\nwhisper-cheap\r\n'
_MODEL_B = b'Content-Disposition: form-data; name="model"\r\n\r\ngpt-4o-transcribe'


@pytest.mark.parametrize(
    "body",
    [
        _MODEL_A + b"--b\r\n" + _MODEL_B.replace(b"\r\n\r\n", b"\r\n\n") + b"\r\n--b--\r\n",
        _MODEL_A + b"--b\r\n" + _MODEL_B,
        # python-multipart reads `--bX` as data; the walk read it as a part
        # whose value swallowed the real second model part.
        _MODEL_A + b"--bX\r\n\r\n--b\r\n" + _MODEL_B + b"\r\n--b--\r\n",
        # Go reads `--b` at the very start of a value as a delimiter.
        b'--b\r\nContent-Disposition: form-data; name="file"\r\n\r\n--b\r\n' + _MODEL_B + b"\r\n"
        + _MODEL_A + b"--b--\r\n",
        # A delimiter line ending in a bare LF, and one not at a line start.
        _MODEL_A.replace(b"--b\r\n", b"--b\n", 1) + b"--b--\r\n",
        b"x" + _MODEL_A + b"--b--\r\n",
    ],
    ids=["no-blank-line", "never-closed", "bX-is-data", "b-starts-a-value", "lf-delimiter", "preamble-glued"],
)
def test_a_body_another_parser_splits_differently_is_refused(body):
    """Each of these hides a second `model` from one parser or the other: the
    gateway authorised and priced one value while the upstream served another."""
    reply = _post_multipart(body, "multipart/form-data; boundary=b")
    assert reply.status_code == 400, reply.text
    assert "RFC 7578" in reply.json()["error"]["message"]


def test_a_well_formed_upload_with_a_preamble_and_epilogue_is_read():
    body = b"preamble\r\n" + _MODEL_A + b"--b--\r\nepilogue"
    assert read_field(body, "multipart/form-data; boundary=b", "model") == "whisper-cheap"
