"""CADL Abstract Syntax Tree node definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Union


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
    # The literal as written ("0.1"), for exact arithmetic (norms 0.1, F.5);
    # None when the node was not parsed from text.
    text: str | None = field(default=None, compare=False, repr=False)


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


@dataclass
class Comprehension:
    """Aggregate argument: ``element for variable in domain``."""
    element: Expression
    variable: str
    domain: Union[RangeExpr, Expression]
    loc: SourceLocation | None = None


Expression = Union[
    IntLiteral, FloatLiteral, BoolLiteral, StringLiteral,
    DurationLiteral, DistLiteral, ListLiteral,
    ActorRef, MemberAccess, FunctionCall,
    BinaryOp, UnaryOp, QuantifiedExpr, Identifier, Comprehension,
]


# === Actor definition ===

@dataclass
class InterfaceDef:
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)


@dataclass
class AgentBinding:
    """`agent:` of an actor (Appendix G, G.3), kept as written."""
    kind: str = ""                     # llm_agent | tool_server | human | service
    protocol: str | None = None        # hyphen_name, e.g. mcp, a2a
    tools: list[str] = field(default_factory=list)
    has_tools: bool = False            # the key `tools:` is present
    # Entries of `tools:` that the YAML loader did not read as a string
    # (an unquoted yes / no / on / off / true / false / null, a number).
    tools_not_names: list[str] = field(default_factory=list)
    # An `agent:` or `tools:` value of the wrong shape, as written.
    invalid: str | None = None
    invalid_tools: str | None = None


@dataclass
class ActorDef:
    id: ActorRef
    role: str
    autonomy: AutonomyLevel
    capabilities: list[str] = field(default_factory=list)
    interface: InterfaceDef | None = None
    loc: SourceLocation | None = None
    # An `autonomy:` value outside low / medium / high, kept as text so
    # that `cadl check` can report it; `autonomy` is then the default.
    invalid_autonomy: str | None = None
    # Agent governance extension (Appendix G): `agent:`, None when absent.
    agent: AgentBinding | None = None


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
    # Entries of `sharing:` that are not a quoted "A -> B : item" string
    # (Appendix A §A.4), kept as text so that `cadl check` can report them.
    invalid_sharing: list[str] = field(default_factory=list)
    # Entries accepted leniently although their item is not an identifier
    # (for example "A -> B : report(period: 5s)"); only the name is kept.
    lenient_sharing: list[str] = field(default_factory=list)


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
    transition: str | None = None      # lifecycle state to move to (not a transition id)
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
    # A `deadline:` that is not a duration ("soon"), kept as written so that
    # `cadl check` can report it; deadline_ms is then None.
    invalid_deadline: str | None = None
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
    # A `sampling:` that is neither `event` nor `periodic(<duration>)`, kept
    # as written so that `cadl check` can report it; kind is then "event".
    invalid: str | None = None


@dataclass
class OnMatchSpec:
    """What a monitor emits when its rule matches."""
    violation: str | None = None       # name of obligation/prohibition
    transition: str | None = None      # lifecycle state to move to (not a transition id)
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


# === Norms extension (Appendix F, norms 0.1) ===
# Obligations, permissions, prohibitions and powers of a contract, and the
# counts-as rules that create its institutional events. Bearers, events,
# contents and conditions are kept as written; `cadl check` reads them.

@dataclass
class NormViolationSpec:
    """`on_violation:` of a norm (F.3)."""
    severity: str = "Major"            # "Minor" | "Major" | "Critical"
    sanction: str | None = None        # text
    reparation: str | None = None      # id of a norm activated on violation
    transition: str | None = None      # lifecycle state to move to


@dataclass
class PowerEffects:
    """`effects:` of a power: the norms each kind of effect applies to."""
    activate: list[str] = field(default_factory=list)
    suspend: list[str] = field(default_factory=list)
    resume: list[str] = field(default_factory=list)
    discharge: list[str] = field(default_factory=list)


# === Agent governance extension (Appendix G, agents 0.1) ===
# `enforcement:` and `delegations:` of a contract and `on_denied:` of a
# prohibition. Actor references and events are kept as written.

@dataclass
class DenialRecovery:
    """`on_denied:` of a prohibition (G.3, G.6)."""
    escalate_to: str | None = None     # actor_ref, as written
    timeout_ms: int | None = None
    # A `timeout:` that is not a duration, kept as written for `cadl check`.
    invalid_timeout: str | None = None
    fallback: str | None = None        # message event (raw)
    notify: str | None = None          # actor_ref, as written
    # An `on_denied:` value that is not a mapping, as written.
    invalid: str | None = None


@dataclass
class EnforcementBlock:
    """`enforcement:` of a contract (G.3); its presence makes the contract
    enforced. `default` and `point` are None when omitted."""
    default: str | None = None         # "deny" | "allow", as written
    point: str | None = None           # "caller" | "receiver" | "gateway", as written
    # An `enforcement:` value that is neither null nor a mapping, as written.
    invalid: str | None = None


@dataclass
class DelegationDef:
    """An entry of `delegations:`: one delegable permission (G.3, G.7)."""
    norm: str = ""                     # id of the permission
    to: list[str] = field(default_factory=list)     # actor_refs, as written
    grant: str = ""
    revoke: str | None = None
    max_depth: int = 1
    # A `max_depth:` that is not an integer of at least 1, as written.
    invalid_max_depth: str | None = None
    until: str | None = None           # norm event (raw)
    # Keys among norm / grant / revoke whose value the YAML loader did not
    # read as a string (an unquoted yes / no / on / off / true / false).
    not_names: list[str] = field(default_factory=list)
    loc: SourceLocation | None = None


@dataclass
class NormDef:
    """An obligation, permission, prohibition or power of one bearer."""
    id: str
    kind: str                          # obligation | permission | prohibition | power
    bearer: str = ""                   # actor_ref, as written
    counterparty: str | None = None
    trigger: str | None = None         # message event, event name or predicate (raw)
    until: str | None = None
    content: str = ""
    deadline_ms: int | None = None
    # A numeric `deadline:` in seconds as written ("0.0005"), which deadline_ms
    # rounds to milliseconds; norms 0.1 compares deadlines exactly (F.5).
    deadline_text: str | None = field(default=None, compare=False, repr=False)
    # A `deadline:` that is not a duration, kept as written for `cadl check`.
    invalid_deadline: str | None = None
    on_violation: NormViolationSpec | None = None
    effects: PowerEffects | None = None
    on_amendment: str = "carry_over"   # "carry_over" | "discharge" | "transform"
    transform_to: str | None = None    # target of `transform:`
    # An `on_amendment:` that is none of the forms of F.3, as written.
    invalid_on_amendment: str | None = None
    # Agent governance extension (Appendix G): `on_denied:`, None when absent.
    on_denied: DenialRecovery | None = None
    loc: SourceLocation | None = None


@dataclass
class CountsAsDef:
    """A counts-as rule: `when` creates the institutional event `creates`."""
    when: str = ""                     # message event or predicate (raw)
    creates: str = ""                  # institutional event name
    if_: str | None = None             # condition (`if:`), raw
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
    # Norms extension (Appendix F)
    norms: list[NormDef] = field(default_factory=list)
    counts_as: list[CountsAsDef] = field(default_factory=list)
    # Agent governance extension (Appendix G); `enforcement` is None for a
    # contract without the key (not enforced).
    enforcement: EnforcementBlock | None = None
    delegations: list[DelegationDef] = field(default_factory=list)
    # A `delegations:` value that is not a list, as written.
    invalid_delegations: str | None = None
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
    # The entries as the YAML loader read them. The stochastic extension
    # needs to know whether an entry is a YAML number (Appendix H, H.3).
    raw: dict[str, Any] = field(default_factory=dict)


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
    # The block as written in the source, for verbatim re-emission (§C.6).
    raw: Any = None
    loc: SourceLocation | None = None


# === Stochastic model extension (Appendix H, stochastic 0.1) ===
#
# The nodes keep the values as the YAML loader read them (``Any``): the
# rules S-1 to S-15 of H.10 are applied by `cadl.stochastic`, which also
# parses the three expression sub-languages of H.2. ``keys`` lists the
# keys written in the mapping, so that an omitted key and the reserved
# keys of H.1 can be told apart.

@dataclass
class ModelParamDef:
    """An entry of `model.parameters:` (H.3)."""
    id: Any = None
    source: Any = None                 # `from:` as written
    range: Any = None
    value: Any = None
    keys: tuple = ()


@dataclass
class ModelVarDef:
    """An entry of `model.variables:` (H.4)."""
    id: Any = None
    type: Any = None
    init: Any = None
    keys: tuple = ()


@dataclass
class ModelUpdateDef:
    """An update of a command (H.5). ``prob`` is None when omitted."""
    prob: Any = None
    set: Any = None
    keys: tuple = ()


@dataclass
class ModelCommandDef:
    """An entry of `model.commands:` (H.5)."""
    action: Any = None
    when: Any = None
    guard: Any = None
    updates: Any = None                # list[ModelUpdateDef], or the value as written
    keys: tuple = ()
    owner: Any = None                  # games 0.1 (Appendix I), as written


@dataclass
class ModelRewardItemDef:
    action: Any = None
    guard: Any = None
    value: Any = None
    keys: tuple = ()


@dataclass
class ModelRewardDef:
    """An entry of `model.rewards:` (H.8)."""
    id: Any = None
    items: Any = None                  # list[ModelRewardItemDef], or the value as written
    keys: tuple = ()


@dataclass
class ModelPropertyDef:
    """An entry of `model.properties:` (H.8)."""
    id: Any = None
    regimes: Any = None
    expr: Any = None
    keys: tuple = ()
    profile: Any = None                # games 0.1 (Appendix I), as written


@dataclass
class ModelRegimeMapDef:
    """An entry of `model.regime_maps:` (H.9)."""
    id: Any = None
    over: Any = None
    regimes: Any = None
    objective: Any = None
    constraint: Any = None
    resolution: Any = None
    keys: tuple = ()
    profile: Any = None                # games 0.1 (Appendix I), as written


@dataclass
class ModelBlock:
    """The `model:` block of an SoS (Appendix H, H.2)."""
    kind: Any = None
    parameters: list[ModelParamDef] = field(default_factory=list)
    variables: list[ModelVarDef] = field(default_factory=list)
    commands: list[ModelCommandDef] = field(default_factory=list)
    labels: Any = None                 # mapping name -> state_pred, as written
    regimes: Any = None                # mapping regime -> {parameters: {...}}, as written
    rewards: list[ModelRewardDef] = field(default_factory=list)
    properties: list[ModelPropertyDef] = field(default_factory=list)
    regime_maps: list[ModelRegimeMapDef] = field(default_factory=list)
    keys: tuple = ()
    # games 0.1 (Appendix I): the values of players:, profiles:, compliance:,
    # payoffs: and equilibria:, as written; `cadl.games` reads them.
    games: dict = field(default_factory=dict)
    # Structural problems found while reading the block (an entry that is
    # not a mapping, ...), as (rule, message) pairs for the type checker.
    malformed: list[tuple[str, str]] = field(default_factory=list)
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
    # `extensions:` declarations as (name, version) pairs (Appendix A §A.2).
    extensions: list[tuple[str, str]] = field(default_factory=list)
    motivation: MotivationBlock | None = None  # Appendix C (v0.1-ext)
    # Stochastic model extension (Appendix H); read whether or not
    # `stochastic: 0.1` is declared, interpreted only when it is.
    model: ModelBlock | None = None
    # Grounding extension (Appendix J): the keys grounding:, shares:,
    # on_fulfilment: and on_violation.magnitude as written
    # (`cadl.grounding.GroundingRaw`), or None when the file uses none.
    grounding: Any = None
    loc: SourceLocation | None = None
