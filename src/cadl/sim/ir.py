"""3-layer Intermediate Representation for CADL simulator config generation.

The IR explicitly separates three research-relevant layers:

Layer 1 — Institution / Governance
    Actors, contracts, governance parameters (alpha/beta/lambda),
    decision authority, information sharing, incentives.

Layer 2 — Interaction Protocol
    Events, message sequences, timing constraints,
    fallback/rollback, preconditions/postconditions.

Layer 3 — Operational Algorithm
    Central planner reference, local planner reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Layer 1: Institution / Governance
# ---------------------------------------------------------------------------

@dataclass
class ActorSpec:
    """An actor template in the SoS."""
    id: str
    role: str
    autonomy: str  # "low" | "medium" | "high"
    count: int | None = None  # None = singleton
    capabilities: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)


@dataclass
class GovernanceParams:
    """Institutional parameters for a contract."""
    alpha: float | None = None      # information transparency [0, 1]
    beta: float | None = None       # authority centralization [0, 1]
    lambda_: float | None = None    # incentive alignment [0, 1]
    decision_holder: str | None = None
    sharing_mode: str | None = None  # e.g. "uplink + broadcast"
    incentive_type: str | None = None


@dataclass
class ContractSpec:
    """A contract between actors with governance parameters."""
    id: str
    parties: list[str] = field(default_factory=list)
    assume: list[str] = field(default_factory=list)
    guarantee: list[str] = field(default_factory=list)
    governance: GovernanceParams = field(default_factory=GovernanceParams)
    violation_detect: str | None = None
    violation_action: str | None = None


@dataclass
class InstitutionLayer:
    """Layer 1: institutional structure and governance."""
    actors: list[ActorSpec] = field(default_factory=list)
    contracts: list[ContractSpec] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Layer 2: Interaction Protocol
# ---------------------------------------------------------------------------

@dataclass
class StepSpec:
    """A single step in a protocol (flattened from AST nesting)."""
    type: str  # "message" | "compute" | "condition" | "barrier"
    sender: str | None = None
    receiver: str | None = None
    content: str = ""
    condition: str | None = None


@dataclass
class ProtocolSpec:
    """An interaction protocol."""
    id: str
    trigger: str = ""
    steps: list[StepSpec] = field(default_factory=list)
    timing: dict[str, str] = field(default_factory=dict)
    fallback: dict[str, str] = field(default_factory=dict)
    precondition: str | None = None
    postcondition: str | None = None


@dataclass
class ProtocolLayer:
    """Layer 2: interaction protocols and events."""
    protocols: list[ProtocolSpec] = field(default_factory=list)
    events: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Layer 3: Operational Algorithm
# ---------------------------------------------------------------------------

@dataclass
class AlgorithmSpec:
    """An algorithm pair (central + local)."""
    name: str
    central: str | None = None
    local: str | None = None


@dataclass
class AlgorithmLayer:
    """Layer 3: operational algorithms."""
    algorithms: list[AlgorithmSpec] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Cross-cutting concerns
# ---------------------------------------------------------------------------

@dataclass
class MetricSpec:
    """A metric definition."""
    id: str
    formula: str | None = None
    target: str | None = None


@dataclass
class TransitionSpec:
    """A regime transition."""
    from_regime: str
    to_regime: str
    condition: str | None = None
    protocol: str | None = None


# ---------------------------------------------------------------------------
# Root IR node
# ---------------------------------------------------------------------------

@dataclass
class SimIR:
    """Root of the 3-layer intermediate representation.

    Attributes:
        name: SoS name.
        sos_type: "Directed" | "Acknowledged" | "Collaborative" | "Virtual".
        environment: key-value environment parameters.
        institution: Layer 1 — actors and contracts with governance.
        protocol: Layer 2 — interaction protocols and events.
        algorithm: Layer 3 — operational algorithms.
        metrics: cross-cutting metric definitions.
        transitions: cross-cutting regime transitions.
    """
    name: str
    sos_type: str = ""
    description: str = ""
    environment: dict[str, Any] = field(default_factory=dict)
    institution: InstitutionLayer = field(default_factory=InstitutionLayer)
    protocol: ProtocolLayer = field(default_factory=ProtocolLayer)
    algorithm: AlgorithmLayer = field(default_factory=AlgorithmLayer)
    metrics: list[MetricSpec] = field(default_factory=list)
    transitions: list[TransitionSpec] = field(default_factory=list)
