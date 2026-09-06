"""Provider errors.

An upstream failure keeps its status code and its message. A client debugging
a rate limit should see the rate limit, not a generic 502.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base for everything this package raises."""


class UnknownProviderError(ProviderError):
    """No provider with that id."""

    def __init__(self, provider_id: str) -> None:
        super().__init__(f"unknown provider {provider_id!r}")
        self.provider_id = provider_id


class MissingKeyError(ProviderError):
    """The referenced env var or secret does not exist.

    Carries the *reference*, which is a name. Never a value.
    """

    def __init__(self, provider_id: str, api_key_ref: str) -> None:
        super().__init__(
            f"provider {provider_id!r}: no value for api_key_ref {api_key_ref!r}; "
            f"set the {api_key_ref} environment variable or add it to secrets.json"
        )
        self.provider_id = provider_id
        self.api_key_ref = api_key_ref


class ProviderNotAdmittingError(ProviderError):
    """Rate limited, over budget, or disabled. Temporary, and it says for how long."""

    def __init__(self, provider_id: str, reason: str, retry_after_s: float | None = None) -> None:
        super().__init__(f"provider {provider_id!r} not admitting: {reason}")
        self.provider_id = provider_id
        self.reason = reason
        self.retry_after_s = retry_after_s


class AdapterUnsupportedError(ProviderError):
    """This provider's wire format has no adapter in this build."""


class UpstreamError(ProviderError):
    """An error from the upstream, with its status code and message preserved.

    ``body`` has already been scrubbed of any key material.
    """

    def __init__(
        self,
        provider_id: str,
        status_code: int,
        message: str,
        *,
        body: str = "",
        error_type: str | None = None,
        error_code: str | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(f"{provider_id} upstream {status_code}: {message}")
        self.provider_id = provider_id
        self.status_code = status_code
        self.message = message
        self.body = body
        self.error_type = error_type
        self.error_code = error_code
        self.retry_after_s = retry_after_s

    def to_openai_error(self) -> dict:
        """OpenAI-shaped error body, for the gateway to hand back verbatim."""
        err: dict = {"message": self.message, "type": self.error_type or "upstream_error"}
        if self.error_code is not None:
            err["code"] = self.error_code
        err["provider"] = self.provider_id
        return {"error": err}
