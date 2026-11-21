"""Dependency injection.

Every dependency is injected. Constructing a gateway with all stubs must work
and is how the tests run, so the gateway is never blocked on another agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from control_plane.contracts import (
    DeploymentPort,
    FitPort,
    LinkPort,
    PlannerPort,
    ProviderPort,
    RegistryPort,
    ResolverPort,
)

from .settings import GatewaySettings


@dataclass
class GatewayDeps:
    """The ports the gateway composes. Defaults are the day-0 stubs."""

    registry: RegistryPort = None  # type: ignore[assignment]
    links: LinkPort = None  # type: ignore[assignment]
    resolver: ResolverPort = None  # type: ignore[assignment]
    fit: FitPort = None  # type: ignore[assignment]
    planner: PlannerPort = None  # type: ignore[assignment]
    deployments: DeploymentPort = None  # type: ignore[assignment]
    providers: ProviderPort = None  # type: ignore[assignment]
    # When True, a missing port is a startup error rather than a stub. For a
    # deployment that means to run for real: silently falling back to fixture
    # data is exactly the C-1 failure mode (a day-0 stub wearing a real
    # gateway's clothes), and strict mode is how a composition root asserts
    # "every port really is wired" instead of hoping it noticed.
    strict: bool = False
    settings: GatewaySettings = field(default_factory=GatewaySettings)

    def __post_init__(self) -> None:
        ports = {
            "registry": self.registry,
            "links": self.links,
            "resolver": self.resolver,
            "fit": self.fit,
            "planner": self.planner,
            "deployments": self.deployments,
            "providers": self.providers,
        }
        if self.strict:
            missing = [name for name, value in ports.items() if value is None]
            if missing:
                raise RuntimeError(
                    "GatewayDeps(strict=True) requires every port to be "
                    "supplied; missing: " + ", ".join(missing)
                )
            return

        from . import stubs

        if self.registry is None:
            self.registry = stubs.StubRegistry()
        if self.links is None:
            self.links = stubs.StubLinks()
        if self.resolver is None:
            self.resolver = stubs.StubResolver()
        if self.fit is None:
            self.fit = stubs.StubFit()
        if self.planner is None:
            self.planner = stubs.StubPlanner()
        if self.deployments is None:
            self.deployments = stubs.StubDeployments()
        if self.providers is None:
            self.providers = stubs.StubProviders()


@dataclass
class GatewayContext:
    """Runtime services, built from the deps at startup."""

    deps: GatewayDeps
    settings: GatewaySettings
    stats: "object"
    admission: "object"
    router: "object"
    proxy: "object"
    metrics: "object"
    breaker: "object" = None
    parking: "object" = None
    retry_budget: "object" = None
    # Durable telemetry. Defaults to the no-op sink so every existing
    # construction of a GatewayContext keeps working unchanged.
    sink: "object" = None
    events: "object" = None
    telemetry: "object" = None
    # The model registry behind GET /api/models. Optional and defaulted for
    # the same reason as the three above: every existing construction of a
    # GatewayContext keeps working, and a coordinator that could not open the
    # database answers that endpoint with a sentence rather than failing to
    # start.
    inventory: "object" = None
    # The standing-alert book. Optional and defaulted like the four above:
    # a gateway built without one answers /api/alerts with an empty set.
    alerts: "object" = None
    started_at: float = 0.0
    # Admins, sessions, keys, limits and pricing (gateway/authz.py). Opened
    # lazily on first use, so building an app touches no disk for them; None
    # only on a GatewayContext built by hand, which no middleware guards.
    auth_stores: "object" = None
    # Spec-decode counters scraped from local engines, attributed to a request
    # after it settles. Optional: None means every row says not_reported.
    engine_metrics: "object" = None
    degraded_startup: list[str] = field(default_factory=list)
    # Refused keys, sessions, joins and origins, as warnings and rate-limited
    # audit rows (gateway/secevents.py). Built here, writing through
    # whatever auth_stores holds at the time of the event.
    secevents: "object" = None
    # The in-process launcher internal_api.create_router installs:
    # `await ctx.launcher(payload, actor=..., request=None) -> (status, body)`,
    # the same path POST /api/deployments takes. None until a router exists.
    launcher: "object" = None
    # The benchmark runner (gateway/bench.py), built by create_router.
    bench: "object" = None
    # Saved and auto-recorded run profiles (gateway/profiles_store.py). Set by
    # app.py; the launch path records runs into it when present.
    profiles: "object" = None

    def __post_init__(self) -> None:
        from control_plane.telemetry import NULL_SINK

        from .secevents import SecurityEvents

        if self.secevents is None:
            self.secevents = SecurityEvents(self._audit_security_event)
        if self.sink is None:
            self.sink = NULL_SINK

    def _audit_security_event(self, *args, **kwargs) -> None:
        if self.auth_stores is None:
            return
        self.auth_stores.auth.audit(*args, **kwargs)
