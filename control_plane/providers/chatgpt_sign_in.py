"""Sign in with ChatGPT plan usage: the pieces of the flow that touch no network.

OpenAI's flow for open-source apps (developers.openai.com/siwc). Authorization
code with PKCE and no client secret, where the first authorisation registers
this install and OpenAI issues it a client id of its own.

The redirect is ``http://127.0.0.1:1455/auth/callback`` -- OpenAI allows only a
loopback address -- so it reaches the machine running the browser, which is
usually not the coordinator. Two ways back, both finishing the same attempt:

* the coordinator answers ``GET /auth/callback`` itself, which is where the
  redirect lands when the browser runs on the coordinator or reaches it through
  ``ssh -L 1455:127.0.0.1:<coordinator port>``;
* or the operator pastes the address -- from the address bar, or from the
  developer tools' network panel when the browser hangs on the redirect.

:class:`ProviderService` owns the attempts and the network calls; this module
builds the URL and checks what comes back.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, unquote_plus, urlencode, urlsplit

import jwt

from .config import (
    CHATGPT_AGENT_NAME,
    CHATGPT_AUTHORIZE_URL,
    CHATGPT_ISSUER,
    CHATGPT_REDIRECT_URI,
    CHATGPT_RESOURCE,
    CHATGPT_SCOPES,
)
from .errors import SignInError

#: The client id a first authorisation registers under. The callback carries
#: the id OpenAI issued in its place, and that one is used from then on.
DYNAMIC_CLIENT_ID = "dynamic_agent_client"

#: Every refusal that means "start over" says the same thing.
ATTEMPT_UNKNOWN = "This sign-in expired or the coordinator restarted. Start again."
BAD_CALLBACK = (
    "That has no sign-in code in it. Paste the address the ChatGPT tab was sent "
    f"to -- it starts with {CHATGPT_REDIRECT_URI}?code= -- from the address bar, "
    "or, if the tab hung instead, from the developer tools' Network panel."
)
NO_PLAN_USAGE = (
    "ChatGPT plan usage was not granted. Sign in again and allow derate to use "
    "your ChatGPT plan."
)
WRONG_ACCOUNT = (
    "That is a different ChatGPT account from the one this provider is signed in as."
)


@dataclass
class SignInAttempt:
    attempt_id: str
    state: str
    nonce: str
    verifier: str
    client_id: str
    #: Set on a sign-in-again: the provider whose record this replaces.
    provider_id: str | None
    email_hint: str | None
    created: float


def new_attempt(
    *, client_id: str, provider_id: str | None, email_hint: str | None, now: float
) -> SignInAttempt:
    return SignInAttempt(
        attempt_id=secrets.token_urlsafe(16),
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        verifier=secrets.token_urlsafe(64),
        client_id=client_id,
        provider_id=provider_id,
        email_hint=email_hint,
        created=now,
    )


def code_challenge(verifier: str) -> str:
    """S256: base64url, no padding, of the verifier's sha256."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(attempt: SignInAttempt, *, host_id: str) -> str:
    params: dict[str, str] = {"client_id": attempt.client_id}
    if attempt.provider_id is None:
        params["agent_name_hint"] = CHATGPT_AGENT_NAME
    params.update(
        {
            "ext_agent_host_id": host_id,
            "response_type": "code",
            "redirect_uri": CHATGPT_REDIRECT_URI,
            "scope": CHATGPT_SCOPES,
            "resource": CHATGPT_RESOURCE,
            "state": attempt.state,
            "nonce": attempt.nonce,
            "code_challenge_method": "S256",
            "code_challenge": code_challenge(attempt.verifier),
        }
    )
    # The account hint only: an id_token_hint would put a token in a URL the
    # UI renders and the operator's browser history keeps.
    if attempt.email_hint:
        params["login_hint"] = attempt.email_hint
    return f"{CHATGPT_AUTHORIZE_URL}?{urlencode(params, quote_via=quote)}"


#: The callback's own fields, wherever they appear in what was pasted.
_CALLBACK_FIELD = re.compile(
    r"(?:^|[?&\s\"'<>(,;])(code|state|client_id|scope|error|error_description)=([^&\s\"'<>#,;)]*)"
)


def parse_callback(pasted: str) -> dict[str, str]:
    """The callback's query, one value per name, from whatever was pasted.

    The address bar is the easy case. When the browser hangs on the redirect
    instead of landing, the address is still in the developer tools -- as the
    request URL, a ``Location:`` header, or a copied cURL command -- so any of
    those is accepted: the fields are found wherever they occur, and the first
    occurrence of each wins.
    """
    text = pasted.strip()
    if "?" in text and "://" in text.split("?", 1)[0]:
        query = parse_qs(urlsplit(text.split()[0].strip("'\"<>")).query)
        found = {name: values[0] for name, values in query.items() if values}
        if "code" in found or "error" in found:
            return found
    found = {}
    for name, value in _CALLBACK_FIELD.findall(text):
        found.setdefault(name, unquote_plus(value))
    return found


def verify_id_token(id_token: str, jwks: dict, *, client_id: str, nonce: str) -> dict:
    """Signature, issuer, audience, expiry and nonce. Any failure is one refusal.

    The key set is fetched by the caller through the service's own client
    rather than by ``PyJWKClient``, which fetches with urllib and would bypass
    the client a test injects.
    """
    try:
        header = jwt.get_unverified_header(id_token)
        keys = jwt.PyJWKSet.from_dict(jwks)
        kid = header.get("kid")
        key = next((k for k in keys.keys if k.key_id == kid), None)
        if key is None:
            raise jwt.InvalidTokenError(f"no key {kid!r} in the issuer's key set")
        claims = jwt.decode(
            id_token,
            key.key,
            algorithms=["RS256", "ES256"],
            audience=client_id,
            issuer=CHATGPT_ISSUER,
            leeway=60,
        )
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise SignInError(
            "sign_in_bad_id_token",
            f"OpenAI's ID token did not verify ({type(exc).__name__}). Start again.",
        ) from None
    if not isinstance(claims.get("nonce"), str) or not secrets.compare_digest(
        claims["nonce"], nonce
    ):
        raise SignInError(
            "sign_in_bad_id_token", "OpenAI's ID token did not match this sign-in. Start again."
        )
    return claims
