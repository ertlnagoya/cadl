"""CADL Abstract Syntax Tree node definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Union


# === Enums ===

class SoSType(Enum):
    DIRECTED = "Directed"
    ACKNOWLEDGED = "Acknowledged"
    COLLABORATIVE = "Collaborative"
    VIRTUAL = "Virtual"


class AutonomyLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# === Location info for error reporting ===

@dataclass
class SourceLocation:
    line: int
    column: int
    end_line: int | None = None
    end_column: int | None = None


# === Literal / Expression nodes ===

@dataclass
class Identifier:
    name: str
    loc: SourceLocation | None = None


@dataclass
class IntLiteral:
    value: int
    loc: SourceLocation | None = None


@dataclass
class FloatLiteral:
    value: float
    loc: SourceLocation | None = None


@dataclass
class BoolLiteral:
    value: bool
    loc: SourceLocation | None = None


@dataclass
class StringLiteral:
    value: str
    loc: SourceLocation | None = None


@dataclass
class DurationLiteral:
    value: int
    unit: str  # "ms", "s", "min", "h"
    loc: SourceLocation | None = None


@dataclass
class DistLiteral:
    """Probability distribution literal, e.g. Normal(15, 3)."""
    name: str
    args: list[Expression]
    loc: SourceLocation | None = None


@dataclass
class ListLiteral:
    items: list[Expression]
    loc: SourceLocation | None = None


@dataclass
class RangeExpr:
    start: int
    end: int | str  # int or identifier
    loc: SourceLocation | None = None


@dataclass
class ActorRef:
    name: str
    index: None | str | int | RangeExpr = None  # None, "*", int, or RangeExpr
    loc: SourceLocation | None = None


@dataclass
class MemberAccess:
    obj: ActorRef
    member: str
    loc: SourceLocation | None = None


@dataclass
class FunctionCall:
    name: str
    args: list[Expression]
    loc: SourceLocation | None = None


@dataclass
class BinaryOp:
    op: str  # +, -, *, /, AND, OR, ==, !=, <, <=, >, >=
    left: Expression
    right: Expression
    loc: SourceLocation | None = None


@dataclass
class UnaryOp:
    op: str  # NOT
    operand: Expression
    loc: SourceLocation | None = None


@dataclass
class QuantifiedExpr:
    quantifier: str  # "for_all" or "exists"
    variable: str
    domain: Expression
    predicate: Expression
    loc: SourceLocation | None = None


Expression = Union[
    IntLiteral, FloatLiteral, BoolLiteral, StringLiteral,
    DurationLiteral, DistLiteral, ListLiteral,
    ActorRef, MemberAccess, FunctionCall,
    BinaryOp, UnaryOp, QuantifiedExpr, Identifier,
]


# === Actor definition ===

@dataclass
class InterfaceDef:
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)


@dataclass
class ActorDef:
    id: ActorRef
    role: str
    autonomy: AutonomyLevel
    capabilities: list[str] = field(default_factory=list)
    interface: InterfaceDef | None = None
    loc: SourceLocation | None = None


# === Contract definition ===

@dataclass
class AuthorityBlock:
    decision_scope: str | None = None
    decision_holder: ActorRef | None = None
    beta: float | None = None
    mode: str | None = None


@dataclass
class ViewDef:
    actor: ActorRef
    view: str


@dataclass
class SharingDef:
    source: ActorRef
    target: ActorRef
    data: str


@dataclass
class InformationBlock:
    alpha: float | None = None
    views: list[ViewDef] = field(default_factory=list)
    sharing: list[SharingDef] = field(default_factory=list)


@dataclass
class ResponsibilityGroup:
    actor: ActorRef
    items: list[str | FunctionCall]


@dataclass
class IncentiveRule:
    description: str


@dataclass
class IncentivesBlock:
    type: str | None = None
    lambda_: float | None = None
    rules: list[IncentiveRule] = field(default_factory=list)


@dataclass
class ViolationBlock:
    detect: str | None = None
    action: str | None = None
    escalation: str | None = None


# === SoS-DSL extension (Appendix E, v0.1-sos-ext) ===
# These nodes promote per-instance contract execution to first-class
# language constructs. Core-conforming processors MAY ignore lifecycle:
# and monitors: but SHOULD preserve them when re-emitting CADL sources.

@dataclass
class OnViolationSpec:
    """Effect of a normative violation on the contract lifecycle."""
    transition: str | None = None      # name of a lifecycle transition to fire
    severity: str = "Major"            # "Minor" | "Major" | "Critical"


@dataclass
class LifecycleTransition:
    """A single lifecycle state transition for a contract instance."""
    id: str
    from_states: list[str]             # post-normalization always a list
    to_state: str
    on: str                            # event expression (raw string)
    when: str | None = None            # optional guard predicate (raw)
    deadline_ms: int | None = None     # normalized from "5s" -> 5000
    on_violation: OnViolationSpec | None = None
    emit: list[str] = field(default_factory=list)
    loc: SourceLocation | None = None


@dataclass
class LifecycleSpec:
    """Per-instance lifecycle of a contract."""
    states: list[str] = field(default_factory=list)
    initial: str | None = None
    terminal: list[str] = field(default_factory=list)
    transitions: list[LifecycleTransition] = field(default_factory=list)
    loc: SourceLocation | None = None


@dataclass
class SamplingSpec:
    """How a monitor samples its observations."""
    kind: str = "event"                # "event" | "periodic"
    period_ms: int | None = None       # only for periodic


@dataclass
class OnMatchSpec:
    """What a monitor emits when its rule matches."""
    violation: str | None = None       # name of obligation/prohibition
    transition: str | None = None      # name of lifecycle transition
    severity: str = "Major"


@dataclass
class MonitorDef:
    """Declarative observation rule attached to a contract."""
    id: str
    observe: list[str] = field(default_factory=list)
    sampling: SamplingSpec = field(default_factory=SamplingSpec)
    rule: str = ""
    on_match: OnMatchSpec | None = None
    loc: SourceLocation | None = None


@dataclass
class ContractDef:
    id: str
    parties: list[ActorRef]
    assume: list[Expression] = field(default_factory=list)
    guarantee: list[Expression] = field(default_factory=list)
    authority: AuthorityBlock | None = None
    information: InformationBlock | None = None
    responsibilities: list[ResponsibilityGroup] = field(default_factory=list)
    incentives: IncentivesBlock | None = None
    violation: ViolationBlock | None = None
    duration: str | None = None
    # SoS-DSL extension (Appendix E)
    lifecycle: LifecycleSpec | None = None
    monitors: list[MonitorDef] = field(default_factory=list)
    loc: SourceLocation | None = None


# === Protocol definition ===

@dataclass
class MessageStep:
    sender: ActorRef
    receiver: ActorRef
    message: Expression
    loc: SourceLocation | None = None


@dataclass
class ComputeStep:
    actor: ActorRef
    computation: Expression
    loc: SourceLocation | None = None


@dataclass
class ConditionalStep:
    condition: Expression
    then_steps: list[Step]
    else_steps: list[Step] = field(default_factory=list)
    loc: SourceLocation | None = None


@dataclass
class ParallelStep:
    steps: list[Step]
    loc: SourceLocation | None = None


@dataclass
class BarrierStep:
    condition: Expression
    loc: SourceLocation | None = None


Step = Union[MessageStep, ComputeStep, ConditionalStep, ParallelStep, BarrierStep]


@dataclass
class TimingBlock:
    entries: dict[str, str] = field(default_factory=dict)


@dataclass
class FallbackBlock:
    entries: dict[str, str] = field(default_factory=dict)


@dataclass
class RollbackBlock:
    condition: str | None = None
    action: str | None = None


@dataclass
class ProtocolDef:
    id: str
    trigger: str
    steps: list[Step] = field(default_factory=list)
    precondition: str | None = None
    postcondition: str | None = None
    timing: TimingBlock | None = None
    fallback: FallbackBlock | None = None
    rollback: RollbackBlock | None = None
    safety_invariant: str | None = None
    loc: SourceLocation | None = None


# === Algorithm, Transition, Metric ===

@dataclass
class AlgorithmDef:
    name: str
    central: str | None = None
    local: str | None = None


@dataclass
class TransitionDef:
    from_regime: str
    to_regime: str
    condition: str | None = None
    protocol: str | None = None
    safety_invariant: str | None = None
    loc: SourceLocation | None = None


@dataclass
class MetricDef:
    id: str
    formula: str | None = None
    target: str | None = None
    loc: SourceLocation | None = None


# === Context ===

@dataclass
class EnvironmentDef:
    entries: dict[str, Expression] = field(default_factory=dict)


@dataclass
class ContextBlock:
    environment: EnvironmentDef | None = None
    assumptions: list[str] = field(default_factory=list)


# === Verification / Codegen specs ===

@dataclass
class VerificationSpec:
    """A verification directive within a CADL definition.

    `method` is the verifier back-end as specified in Appendix A §A.8:
    one of ``"smt"``, ``"model_check"``, ``"simulation"``, ``"proof"``.
    Defaults to ``"smt"`` when unset. Unknown methods are preserved
    verbatim for downstream tools but the reference verifier will emit
    a ``not_supported`` result (see ``cadl.verifier.dispatch_spec``).
    """
    id: str
    type: str  # "consistency", "deadlock", "safety", "liveness"
    target: str | None = None
    property: str | None = None
    method: str | None = None  # "smt" | "model_check" | "simulation" | "proof"
    expr: str | None = None
    bound: int | None = None
    loc: SourceLocation | None = None


@dataclass
class CodegenSpec:
    """A code generation directive within a CADL definition.

    Recognised targets per Appendix A §A.9 and Appendix D:
    ``"unity" | "ros2" | "python" | "solidity" | "opa" | <identifier>``.
    """
    target: str
    output: str | None = None
    mappings: dict[str, str] = field(default_factory=dict)
    loc: SourceLocation | None = None


# === Motivation extension (Appendix C, v0.1-ext) ===

@dataclass
class AgentMotivationBlock:
    """Per-actor motivation specification (Appendix C §C.2)."""
    profile: str = "uniform"  # "uniform" | "linear" | "polarized" | "custom"
    values: list[float] = field(default_factory=list)


@dataclass
class GovernanceMotivationBlock:
    """Governance-level motivation interpretation (Appendix C §C.2)."""
    model: str = "none"  # "none" | "commitment_budget" | "hybrid"
    rho: float = 0.0
    kappa: float = 5.0
    budget_base: int = 3
    wait_scale: float = 3.0


@dataclass
class MotivationBlock:
    """Optional `motivation:` section — CADL v0.1-ext (Appendix C).

    Core-conforming processors MAY ignore this block but SHOULD preserve
    it verbatim when re-emitting CADL sources.
    """
    agent: AgentMotivationBlock | None = None
    governance: GovernanceMotivationBlock | None = None
    loc: SourceLocation | None = None


# === Top-level SoS ===

@dataclass
class SoSDefinition:
    name: str
    type: SoSType | None = None
    version: str | None = None
    description: str | None = None
    context: ContextBlock | None = None
    actors: list[ActorDef] = field(default_factory=list)
    contracts: list[ContractDef] = field(default_factory=list)
    protocols: list[ProtocolDef] = field(default_factory=list)
    algorithms: list[AlgorithmDef] = field(default_factory=list)
    transitions: list[TransitionDef] = field(default_factory=list)
    metrics: list[MetricDef] = field(default_factory=list)
    verifications: list[VerificationSpec] = field(default_factory=list)
    codegen: list[CodegenSpec] = field(default_factory=list)
    motivation: MotivationBlock | None = None  # Appendix C (v0.1-ext)
    loc: SourceLocation | None = None
