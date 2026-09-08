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
    started_at: float = 0.0
    degraded_startup: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        from control_plane.telemetry import NULL_SINK

        if self.sink is None:
            self.sink = NULL_SINK
