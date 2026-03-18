"""Lower CADL AST (SoSDefinition) to the 3-layer Simulator IR."""

from __future__ import annotations

from typing import Any

from ..ast_nodes import (
    ActorRef,
    BarrierStep,
    BinaryOp,
    BoolLiteral,
    ComputeStep,
    ConditionalStep,
    DurationLiteral,
    Expression,
    FloatLiteral,
    FunctionCall,
    Identifier,
    IntLiteral,
    MemberAccess,
    MessageStep,
    ParallelStep,
    QuantifiedExpr,
    RangeExpr,
    SoSDefinition,
    StringLiteral,
    UnaryOp,
)
from .ir import (
    ActorSpec,
    AlgorithmLayer,
    AlgorithmSpec,
    ContractSpec,
    GovernanceParams,
    InstitutionLayer,
    MetricSpec,
    ProtocolLayer,
    ProtocolSpec,
    SimIR,
    StepSpec,
    TransitionSpec,
)


def lower_to_ir(sos: SoSDefinition) -> SimIR:
    """Convert a parsed SoSDefinition AST into the 3-layer SimIR."""
    return SimIR(
        name=sos.name,
        sos_type=sos.type.value if sos.type else "",
        description=sos.description or "",
        environment=_lower_environment(sos),
        institution=_lower_institution(sos),
        protocol=_lower_protocol(sos),
        algorithm=_lower_algorithm(sos),
        metrics=[
            MetricSpec(id=m.id, formula=m.formula, target=m.target)
            for m in sos.metrics
        ],
        transitions=[
            TransitionSpec(
                from_regime=t.from_regime,
                to_regime=t.to_regime,
                condition=t.condition,
                protocol=t.protocol,
            )
            for t in sos.transitions
        ],
    )


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def _lower_environment(sos: SoSDefinition) -> dict[str, Any]:
    env: dict[str, Any] = {}
    if sos.context and sos.context.environment:
        for key, expr in sos.context.environment.entries.items():
            env[key] = _expr_to_value(expr)
    return env


def _expr_to_value(expr: Expression) -> Any:
    """Convert an expression to a plain Python value for environment params."""
    if isinstance(expr, IntLiteral):
        return expr.value
    if isinstance(expr, FloatLiteral):
        return expr.value
    if isinstance(expr, BoolLiteral):
        return expr.value
    if isinstance(expr, StringLiteral):
        return expr.value
    return _expr_to_str(expr)


# ---------------------------------------------------------------------------
# Layer 1: Institution
# ---------------------------------------------------------------------------

def _lower_institution(sos: SoSDefinition) -> InstitutionLayer:
    actors = [_lower_actor(a) for a in sos.actors]
    contracts = [_lower_contract(c) for c in sos.contracts]
    return InstitutionLayer(actors=actors, contracts=contracts)


def _lower_actor(actor) -> ActorSpec:
    ref = actor.id
    count = None
    actor_id = ref.name

    if isinstance(ref.index, RangeExpr):
        end = ref.index.end
        start = ref.index.start
        if isinstance(end, int) and isinstance(start, int):
            count = end - start + 1
        elif isinstance(end, str):
            # Symbolic like 1..N — keep as None, note in id
            count = None
        else:
            count = None
    elif isinstance(ref.index, int):
        count = 1

    inputs: list[str] = []
    outputs: list[str] = []
    if actor.interface:
        inputs = list(actor.interface.inputs)
        outputs = list(actor.interface.outputs)

    return ActorSpec(
        id=actor_id,
        role=actor.role,
        autonomy=actor.autonomy.value,
        count=count,
        capabilities=list(actor.capabilities),
        inputs=inputs,
        outputs=outputs,
    )


def _lower_contract(contract) -> ContractSpec:
    governance = GovernanceParams()

    # Extract authority params
    if contract.authority:
        governance.beta = contract.authority.beta
        if contract.authority.decision_holder:
            governance.decision_holder = _actor_ref_str(
                contract.authority.decision_holder
            )

    # Extract information params
    if contract.information:
        governance.alpha = contract.information.alpha
        if contract.information.sharing:
            parts = []
            for s in contract.information.sharing:
                src = _actor_ref_str(s.source)
                tgt = _actor_ref_str(s.target)
                parts.append(f"{src} -> {tgt} : {s.data}")
            governance.sharing_mode = " ; ".join(parts)

    # Extract incentive params
    if contract.incentives:
        governance.lambda_ = contract.incentives.lambda_
        governance.incentive_type = contract.incentives.type

    return ContractSpec(
        id=contract.id,
        parties=[_actor_ref_str(p) for p in contract.parties],
        assume=[_expr_to_str(e) for e in contract.assume],
        guarantee=[_expr_to_str(e) for e in contract.guarantee],
        governance=governance,
        violation_detect=contract.violation.detect if contract.violation else None,
        violation_action=contract.violation.action if contract.violation else None,
    )


# ---------------------------------------------------------------------------
# Layer 2: Protocol
# ---------------------------------------------------------------------------

def _lower_protocol(sos: SoSDefinition) -> ProtocolLayer:
    protocols = []
    events: list[str] = []

    for p in sos.protocols:
        spec = ProtocolSpec(
            id=p.id,
            trigger=p.trigger,
            steps=_flatten_steps(p.steps),
            timing=dict(p.timing.entries) if p.timing else {},
            fallback=dict(p.fallback.entries) if p.fallback else {},
            precondition=p.precondition,
            postcondition=p.postcondition,
        )
        protocols.append(spec)
        if p.trigger and p.trigger not in events:
            events.append(p.trigger)

    return ProtocolLayer(protocols=protocols, events=events)


def _flatten_steps(steps) -> list[StepSpec]:
    """Recursively flatten AST steps into a flat list of StepSpec."""
    result: list[StepSpec] = []
    for step in steps:
        if isinstance(step, MessageStep):
            result.append(StepSpec(
                type="message",
                sender=_actor_ref_str(step.sender),
                receiver=_actor_ref_str(step.receiver),
                content=_expr_to_str(step.message),
            ))
        elif isinstance(step, ComputeStep):
            result.append(StepSpec(
                type="compute",
                sender=_actor_ref_str(step.actor),
                content=_expr_to_str(step.computation),
            ))
        elif isinstance(step, ConditionalStep):
            result.append(StepSpec(
                type="condition",
                content="if",
                condition=_expr_to_str(step.condition),
            ))
            result.extend(_flatten_steps(step.then_steps))
            if step.else_steps:
                result.append(StepSpec(type="condition", content="else"))
                result.extend(_flatten_steps(step.else_steps))
        elif isinstance(step, ParallelStep):
            result.append(StepSpec(type="condition", content="parallel_begin"))
            result.extend(_flatten_steps(step.steps))
            result.append(StepSpec(type="condition", content="parallel_end"))
        elif isinstance(step, BarrierStep):
            result.append(StepSpec(
                type="barrier",
                content=_expr_to_str(step.condition),
            ))
    return result


# ---------------------------------------------------------------------------
# Layer 3: Algorithm
# ---------------------------------------------------------------------------

def _lower_algorithm(sos: SoSDefinition) -> AlgorithmLayer:
    return AlgorithmLayer(
        algorithms=[
            AlgorithmSpec(name=a.name, central=a.central, local=a.local)
            for a in sos.algorithms
        ]
    )


# ---------------------------------------------------------------------------
# Helpers: expression / actor-ref stringification
# ---------------------------------------------------------------------------

def _actor_ref_str(ref: ActorRef) -> str:
    """Convert an ActorRef AST node to a readable string."""
    if ref.index is None:
        return ref.name
    if ref.index == "*":
        return f"{ref.name}[*]"
    if isinstance(ref.index, int):
        return f"{ref.name}[{ref.index}]"
    if isinstance(ref.index, RangeExpr):
        end = ref.index.end
        return f"{ref.name}[{ref.index.start}..{end}]"
    return ref.name


def _expr_to_str(expr: Expression) -> str:
    """Convert an AST expression node back to a readable string."""
    if isinstance(expr, Identifier):
        return expr.name
    if isinstance(expr, IntLiteral):
        return str(expr.value)
    if isinstance(expr, FloatLiteral):
        return str(expr.value)
    if isinstance(expr, BoolLiteral):
        return str(expr.value).lower()
    if isinstance(expr, StringLiteral):
        return expr.value
    if isinstance(expr, DurationLiteral):
        return f"{expr.value}{expr.unit}"
    if isinstance(expr, ActorRef):
        return _actor_ref_str(expr)
    if isinstance(expr, MemberAccess):
        return f"{_actor_ref_str(expr.obj)}.{expr.member}"
    if isinstance(expr, FunctionCall):
        args = ", ".join(_expr_to_str(a) for a in expr.args)
        return f"{expr.name}({args})"
    if isinstance(expr, BinaryOp):
        left = _expr_to_str(expr.left)
        right = _expr_to_str(expr.right)
        return f"{left} {expr.op} {right}"
    if isinstance(expr, UnaryOp):
        return f"{expr.op} {_expr_to_str(expr.operand)}"
    if isinstance(expr, QuantifiedExpr):
        domain = _expr_to_str(expr.domain)
        pred = _expr_to_str(expr.predicate)
        q = "for_all" if expr.quantifier == "for_all" else "exists"
        return f"{q} {expr.variable} in {domain}: {pred}"
    # Fallback
    return str(expr)
