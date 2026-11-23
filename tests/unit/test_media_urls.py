"""A /v1 chat body's media URLs must name a public host.

The model node fetches them itself, from inside the LAN, so a URL naming that
LAN (or loopback, or the metadata service) is refused before anything is
forwarded. ``data:`` always passes; a name is never resolved.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from control_plane.gateway import create_app
from control_plane.gateway.media import media_url_refusal
from tests.unit.test_gateway import (
    FakeBackend,
    FakeDeployments,
    RunningBackend,
    build_deps,
    make_deployment,
)


def _chat(*parts) -> dict:
    return {"model": "m", "messages": [{"role": "user", "content": list(parts)}]}


def _image(url) -> dict:
    return {"type": "image_url", "image_url": {"url": url}}


@pytest.mark.parametrize(
    "url",
    [
        "data:image/png;base64,iVBORw0KGgo=",
        "https://example.com/a.png",
        "http://93.184.215.14/a.png",
        "https://[2606:4700::6810:84e5]/a.png",
        # Lookalikes of the refused shapes that are in fact public.
        "https://b\u00fccher.example/a.png",  # IDN
        "https://deadbeef.cafe/a.png",  # hex-looking name, not an address
        "https://user@example.com/a.png",
        " data:image/png;base64,iVBORw0KGgo=",
    ],
)
def test_public_and_inline_media_pass(url):
    assert media_url_refusal(_chat(_image(url))) is None


@pytest.mark.parametrize(
    "url,host",
    [
        ("http://10.0.0.5/x", "10.0.0.5"),
        ("http://127.0.0.1:8081/agent/health", "127.0.0.1"),
        ("http://[::1]/", "::1"),
        ("http://169.254.169.254/latest/meta-data", "169.254.169.254"),
        ("http://spark-01/", "spark-01"),
        ("http://nas.local/", "nas.local"),
        ("http://LOCALHOST./x", "localhost"),
        ("http://box.home.arpa/x", "box.home.arpa"),
        ("http://[::ffff:192.168.0.1]/x", "::ffff:192.168.0.1"),
        # inet_aton's legacy spellings: what a glibc resolver connects to
        # without ever asking DNS.
        ("http://127.1/", "127.1"),
        ("http://0x7f.0.0.1/", "0x7f.0.0.1"),
        ("http://0177.0.0.1/", "0177.0.0.1"),
        ("http://2130706433/", "2130706433"),
        ("http://0x7f000001/", "0x7f000001"),
        ("http://[64:ff9b::a00:1]/", "64:ff9b::a00:1"),  # NAT64 of 10.0.0.1
        ("http://224.0.0.1/", "224.0.0.1"),
        ("http://100.64.0.1/", "100.64.0.1"),
        ("http://0.0.0.0/", "0.0.0.0"),
        ("http://example.com@127.0.0.1/", "127.0.0.1"),
        ("http://10.0.0.1#@example.com/", "10.0.0.1"),
        ("HTTP://10.0.0.1/", "10.0.0.1"),
        # IDNA folds these into ASCII before the fetcher resolves them.
        ("http://a.local\u3002/", "a.local"),
        ("http://127\u30020\u30020\u30021/", "127.0.0.1"),
        ("http://\uff4c\uff4f\uff43\uff41\uff4c\uff48\uff4f\uff53\uff54/", "localhost"),
        ("http://metadata.google.internal/computeMetadata/v1/", "metadata.google.internal"),
    ],
)
def test_a_non_public_host_is_refused_by_name(url, host):
    assert media_url_refusal(_chat(_image(url))) == (
        f"Media URLs must point at a public host; {host!r} is not one."
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1\\@example.com/",  # WHATWG: host 127.0.0.1
        "http://%31%30.0.0.1/",  # decoded by some fetchers to 10.0.0.1
        "http://exa mple.com@10.0.0.1/",
        "http://[fe80::1%25eth0]/",
    ],
)
def test_an_authority_two_parsers_could_read_differently_is_refused(url):
    """The host this module checks must be the host the fetcher connects to.
    A backslash, a percent escape or whitespace in the authority is where URL
    parsers disagree, so it is refused rather than interpreted."""
    why = media_url_refusal(_chat(_image(url)))
    assert why is not None and "plainly" in why, why


@pytest.mark.parametrize(
    "url",
    ["http://192.168.0.1.nip.io/", "http://localtest.me/", "http://box.corp/"],
)
@pytest.mark.xfail(strict=True, reason="names are not resolved: DNS that answers privately is out of reach")
def test_a_public_name_that_resolves_privately_is_not_caught(url):
    assert media_url_refusal(_chat(_image(url))) is not None


@pytest.mark.parametrize("url,scheme", [("file:///etc/passwd", "file"), ("ftp://example.com/x", "ftp")])
def test_a_scheme_other_than_http_or_data_is_refused(url, scheme):
    assert media_url_refusal(_chat(_image(url))) == (
        f"Media URLs must be http(s) or data:; got scheme {scheme!r}."
    )


@pytest.mark.parametrize("url", ["http://[x/", "http://[127.0.0.1]/", "http://[foo]/"])
def test_a_url_the_parser_raises_on_is_refused_not_a_500(url):
    body = {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}]}
    assert media_url_refusal(body)


def test_every_part_shape_is_checked():
    lan = "http://192.168.0.9/x"
    assert media_url_refusal(_chat({"type": "image_url", "image_url": lan}))
    assert media_url_refusal(_chat({"type": "video_url", "video_url": {"url": lan}}))
    assert media_url_refusal(_chat({"type": "audio_url", "audio_url": {"url": lan}}))
    # vLLM's plain-string forms of the same three parts.
    assert media_url_refusal(_chat({"type": "video_url", "video_url": lan}))
    assert media_url_refusal(_chat({"type": "audio_url", "audio_url": lan}))
    assert media_url_refusal(_chat({"type": "input_image", "input_image": lan}))
    assert media_url_refusal(_chat({"type": "input_image", "input_image": {"url": lan}}))
    assert media_url_refusal(_chat({"type": "input_image", "image_url": lan}))
    # The first offending part decides, after a passing one.
    assert media_url_refusal(_chat(_image("https://example.com/a.png"), _image(lan)))


def test_shapes_this_does_not_own_are_left_to_the_backend():
    assert media_url_refusal(_chat({"type": "image_url", "image_url": {"detail": "low"}})) is None
    assert media_url_refusal(_chat({"type": "image_url", "image_url": {"url": 7}})) is None
    assert media_url_refusal({"model": "m", "messages": [{"role": "user", "content": "http://10.0.0.1/"}]}) is None
    assert media_url_refusal({"model": "m", "input": "x"}) is None


def test_a_lan_image_is_refused_through_v1_and_never_forwarded():
    backend = FakeBackend()
    with RunningBackend(backend) as running:
        app = create_app(build_deps(deployments=FakeDeployments(
            [make_deployment("d-1", "llama", backend_url=running.base_url)]
        )))
        with TestClient(app) as client:
            refused = client.post(
                "/v1/chat/completions",
                json={**_chat(_image("http://192.168.0.69:18081/agent/health")), "model": "llama"},
            )
            assert backend.requests == []
            passed = client.post(
                "/v1/chat/completions",
                json={**_chat(_image("data:image/png;base64,iVBORw0KGgo=")), "model": "llama"},
            )
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "media_url_refused"
    assert "'192.168.0.69'" in refused.json()["error"]["message"]
    assert passed.status_code == 200, passed.text
    assert len(backend.requests) == 1


def test_a_refusal_does_not_echo_an_unbounded_url():
    """A 25 MiB URL must not come back as a 25 MiB error body."""
    why = media_url_refusal(_chat(_image("http:///" + "a" * 100_000)))
    assert why is not None and len(why) < 400
