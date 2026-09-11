"""An optional bearer-token gate on `/api`.

The gateway has no per-request credential by default (00-architecture.md:
"the /api surface has no authentication") -- `csrf.py` only stops a browser
from being tricked into a same-origin-looking write; it does nothing against
someone simply handed the URL. This is the stopgap for that: set
`DERATE_API_TOKEN` and every `/api` request must carry a matching
`Authorization: Bearer <token>` header, or be refused before it reaches a
route.

Unset (the default) is a no-op, deliberately: every existing workflow that
talks to `/api` without a token today -- `ui/check.mjs`, `tests/model_sweep`,
`tests/load/loadtest.py`, this project's own documented curl workflows --
keeps working exactly as it does now. This is additive, never a default.

`/v1` is not guarded here. It is the OpenAI-compatible model surface, meant
to be reachable the way any OpenAI SDK client reaches one; gating it is a
bigger, separate decision this stopgap does not make.

One path stays reachable even when a token is set: `POST /api/nodes/join`. A
joining node authenticates with its own short-lived enrollment token, minted
by an already-authenticated operator through `POST /api/enroll` and carried
in the join body, not this header. Requiring the permanent management token
as well would mean shipping that secret to every new machine on the install
command, which defeats the point of having a separate, revocable,
single-purpose token for that step.
"""

from __future__ import annotations

GUARDED_PREFIX = "/api"

#: See the module docstring: a joining node presents its own enrollment
#: token in the request body, not this header.
EXEMPT_PATHS = frozenset({"/api/nodes/join"})


def is_unauthorized(*, path: str, authorization: str | None, token: str | None) -> bool:
    """True when this request must be refused before it reaches a route."""
    if not token:
        return False
    if not path.startswith(GUARDED_PREFIX) or path in EXEMPT_PATHS:
        return False
    if authorization is None:
        return True
    scheme, _, value = authorization.partition(" ")
    return scheme.lower() != "bearer" or value != token
