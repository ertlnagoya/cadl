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

Cross-cutting:
    Task Arbitration  — FCFS delivery assignment, claim resolution, retirement.
    Motivation Config — agent motivation delays, max deliveries, wandering goal mode.
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
    lambda_: float | None = None    # incentive strength [0, 1]
    decision_holder: str | None = None
    sharing_mode: str | None = None  # e.g. "uplink + broadcast"
    incentive_type: str | None = None


# --- SoS-DSL extension (Appendix E) IR specs --------------------------------

@dataclass
class LifecycleTransitionSpec:
    """Lifecycle transition in normalized IR form."""
    id: str
    from_states: list[str] = field(default_factory=list)
    to_state: str = ""
    on: str = ""
    when: str | None = None
    deadline_ms: int | None = None
    on_violation_transition: str | None = None
    on_violation_severity: str | None = None
    emit: list[str] = field(default_factory=list)


@dataclass
class LifecycleSpecIR:
    """Per-instance lifecycle in normalized IR form."""
    states: list[str] = field(default_factory=list)
    initial: str | None = None
    terminal: list[str] = field(default_factory=list)
    transitions: list[LifecycleTransitionSpec] = field(default_factory=list)


@dataclass
class MonitorSpecIR:
    """Declarative observation rule in normalized IR form."""
    id: str
    observe: list[str] = field(default_factory=list)
    sampling_kind: str = "event"      # "event" | "periodic"
    sampling_period_ms: int | None = None
    rule: str = ""
    on_match_violation: str | None = None
    on_match_transition: str | None = None
    on_match_severity: str | None = None


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
    # SoS-DSL extension (Appendix E)
    lifecycle: LifecycleSpecIR | None = None
    monitors: list[MonitorSpecIR] = field(default_factory=list)


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
class TaskArbitrationSpec:
    """FCFS task arbitration configuration (DELIVERY_ASSIGNMENT protocol).

    Maps directly to the Unity ``taskArbitration`` JSON block.
    """
    enabled: bool = False
    protocol: str = "fcfs"            # "fcfs" | "priority" | ...
    max_claim_delay_sec: float = 5.0
    delivery_interval_sec: float = 1.0
    goal_sequence: list[int] = field(default_factory=list)
    startup_delay_sec: float = 0.0
    parallel: bool = True
    deadlock_recovery_enabled: bool = False
    deadlock_detection_sec: float = 15.0
    claim_resolution: str = "all-robot-wait"  # "all-robot-wait" | "first-come"


@dataclass
class MotivationSpec:
    """Agent motivation and wandering-goal configuration.

    Maps directly to the Unity ``motivationConfig`` JSON block.

    agent_motivation:     per-robot claim-delay weight (0 = first to claim).
    max_deliveries:       per-robot delivery cap before retirement.
    wandering_goal_mode:  "random" (default) | "select" (deterministic list).
    wandering_goal_list:  ordered goal IDs used when wandering_goal_mode == "select".
    """
    enabled: bool = False
    model: str = "none"               # "none" | "fatigue" | ...
    agent_motivation: list[float] = field(default_factory=list)
    max_deliveries: list[int] = field(default_factory=list)
    wandering_goal_mode: str = "random"
    wandering_goal_list: list[int] = field(default_factory=list)


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
        task_arbitration: optional FCFS task arbitration config (→ Unity taskArbitration).
        motivation: optional agent motivation / wandering-goal config (→ Unity motivationConfig).
        motivation_block: the `motivation:` block of the source, kept verbatim (Appendix C).
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
    task_arbitration: TaskArbitrationSpec | None = None
    motivation: MotivationSpec | None = None
    # The source's `motivation:` block of Appendix C, verbatim (§C.6).
    # Absent from the serialized IR when the source has no such block.
    motivation_block: Any = None
