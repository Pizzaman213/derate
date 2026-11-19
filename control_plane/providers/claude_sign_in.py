"""Pure helpers for the explicitly unofficial Claude subscription connection.

Protocol evidence: ex-machina-co/opencode-anthropic-auth at
93cdd23b95f84a1d495744c2e6cc4058b5a86381. No CLI credentials are read.
Anthropic does not permit third-party routing of subscription credentials.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

from .config import (
    CLAUDE_AUTHORIZE_URL, CLAUDE_CLIENT_ID, CLAUDE_CODE_VERSION,
    CLAUDE_REDIRECT_URI, CLAUDE_SCOPES,
)
from .errors import SignInError

ATTEMPT_UNKNOWN = "This sign-in expired or the coordinator restarted. Start again."
BAD_CALLBACK = "Paste the Claude authorization code as code#state, or the complete callback URL containing code and state."


@dataclass
class ClaudeSignInAttempt:
    attempt_id: str
    state: str
    verifier: str
    provider_id: str | None
    created: float


def new_attempt(*, provider_id: str | None, now: float) -> ClaudeSignInAttempt:
    return ClaudeSignInAttempt(secrets.token_urlsafe(16), secrets.token_urlsafe(32),
                               secrets.token_urlsafe(64), provider_id, now)


def authorize_url(attempt: ClaudeSignInAttempt) -> str:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(attempt.verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return CLAUDE_AUTHORIZE_URL + "?" + urlencode({
        "code": "true", "client_id": CLAUDE_CLIENT_ID, "response_type": "code",
        "redirect_uri": CLAUDE_REDIRECT_URI, "scope": CLAUDE_SCOPES,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": attempt.state,
    })


def parse_callback(value: str) -> dict[str, str]:
    def bad() -> SignInError:
        return SignInError("sign_in_bad_callback", BAD_CALLBACK)
    if not isinstance(value, str):
        raise bad()
    text = value.strip()
    if not text or re.search(r"\s", text) or re.search(r"%(?![0-9a-fA-F]{2})", text):
        raise bad()
    try:
        if "://" in text:
            parts = urlsplit(text)
            if parts.scheme not in ("https", "http") or not parts.netloc or parts.fragment:
                raise bad()
            query = parts.query
        elif "#" in text:
            if text.count("#") != 1 or any(c in text for c in "?&="):
                raise bad()
            code, state = text.split("#")
            if not code or not state:
                raise bad()
            return {"code": code, "state": state}
        else:
            query = text
        fields = parse_qs(query, keep_blank_values=True, strict_parsing=True,
                          encoding="utf-8", errors="strict")
    except (ValueError, UnicodeError):
        raise bad() from None
    if any(len(fields.get(k, [])) != 1 or not fields[k][0] for k in ("code", "state")):
        raise bad()
    if any(re.search(r"[\s#]", fields[k][0]) for k in ("code", "state")):
        raise bad()
    return {k: fields[k][0] for k in ("code", "state")}


def oauth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20"}


def _js_utf8(text: str) -> bytes:
    # JS TextEncoder/createHash replaces lone UTF-16 surrogates with U+FFFD.
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace").encode("utf-8")


def billing_prefix(text: str) -> str:
    units = text.encode("utf-16-le", "surrogatepass")
    sampled = "".join(chr(int.from_bytes(units[i * 2:i * 2 + 2], "little"))
                      if i * 2 < len(units) else "0" for i in (4, 7, 20))
    suffix = hashlib.sha256(_js_utf8("59cf53e54c78" + sampled + CLAUDE_CODE_VERSION)).hexdigest()[:3]
    cch = hashlib.sha256(_js_utf8(text)).hexdigest()[:5]
    return (f"x-anthropic-billing-header: cc_version={CLAUDE_CODE_VERSION}.{suffix}; "
            f"cc_entrypoint=sdk-cli; cch={cch};")


def subscription_request(payload: dict) -> dict:
    first = next((m for m in payload["messages"] if m["role"] == "user"), {})
    text = next((b["text"] for b in first.get("content", []) if b.get("type") == "text"), "")
    # Unofficial wire compatibility identity, NOT a claim that derate is the
    # Anthropic CLI/SDK. Keep caller text unchanged; do not sanitize brands.
    return {**payload, "system": [
        {"type": "text", "text": billing_prefix(text)},
        {"type": "text", "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK."},
        *payload.get("system", []),
    ]}


def inference_headers(token: str) -> dict[str, str]:
    return {**oauth_headers(token),
            "anthropic-beta": "oauth-2025-04-20,interleaved-thinking-2025-05-14",
            "User-Agent": f"claude-cli/{CLAUDE_CODE_VERSION} (external, cli)"}
