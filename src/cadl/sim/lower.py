"""Lower CADL AST (SoSDefinition) to the 3-layer Simulator IR."""

from __future__ import annotations

import copy

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
    CountsAsSpecIR,
    DelegationSpecIR,
    GovernanceParams,
    InstitutionLayer,
    LifecycleSpecIR,
    LifecycleTransitionSpec,
    MetricSpec,
    MonitorSpecIR,
    NormSpecIR,
    ProtocolLayer,
    ProtocolSpec,
    SimIR,
    StepSpec,
    TransitionSpec,
)


def lower_to_ir(sos: SoSDefinition) -> SimIR:
    """Convert a parsed SoSDefinition AST into the 3-layer SimIR."""
    ir = SimIR(
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
        motivation_block=(
            copy.deepcopy(sos.motivation.raw)
            if sos.motivation is not None and sos.motivation.raw is not None
            else None
        ),
        agents_extension=_agents_active(sos),
        stochastic_model=_lower_stochastic_model(sos),
    )
    ir.grounding_extension = _lower_grounding(sos, ir.institution)
    return ir


def _agents_active(sos: SoSDefinition) -> bool:
    from ..agents import agents_active
    return agents_active(sos)


def _lower_grounding(sos: SoSDefinition, institution) -> bool:
    """The fields of the grounding extension (Appendix J, J.10): the field
    `grounding` of every contract and four fields of every norm. Returns
    whether the extension is active; nothing is written when it is not."""
    from ..grounding import annotations, grounding_active, ir_contract, ir_norm_fields
    if not grounding_active(sos):
        return False
    for contract, spec in zip(sos.contracts, institution.contracts):
        spec.grounding = ir_contract(sos, contract)
        for norm_spec, ann in zip(spec.norms, annotations(sos, contract).norms):
            for key, value in ir_norm_fields(ann).items():
                setattr(norm_spec, key, value)
    return True


def _lower_stochastic_model(sos: SoSDefinition) -> Any:
    """The `stochastic_model` field (Appendix H, H.13): the model: block of
    a file that declares `stochastic: 0.1`, or None. A block that breaks a
    rule of H.10 is not interpreted and is None as well; `cadl check`
    reports why."""
    from ..stochastic import checked_model, model_to_ir
    model = checked_model(sos)
    return None if model is None else model_to_ir(model)


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
    from ..agents import agents_active
    from ..norms import norms_declared
    # Without `norms: 0.1` the norms: and counts_as: blocks are not
    # interpreted and stay out of the IR (Appendix F, F.11); likewise the
    # keys of the agent governance extension without `agents: 0.1`
    # (Appendix G, G.14).
    with_norms = norms_declared(sos)
    with_agents = agents_active(sos)
    actors = [_lower_actor(a, with_agents) for a in sos.actors]
    contracts = [_lower_contract(c, with_norms, with_agents) for c in sos.contracts]
    return InstitutionLayer(actors=actors, contracts=contracts)


def _lower_actor(actor, with_agents: bool = False) -> ActorSpec:
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

    agent = actor.agent if with_agents else None
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
        agent_kind=(agent.kind or None) if agent else None,
        agent_protocol=agent.protocol if agent else None,
        agent_tools=list(agent.tools) if agent else [],
    )


def _lower_contract(
    contract, with_norms: bool = False, with_agents: bool = False,
) -> ContractSpec:
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

    from ..agents import DEFAULT_POINT
    enf = contract.enforcement if with_agents else None

    return ContractSpec(
        id=contract.id,
        parties=[_actor_ref_str(p) for p in contract.parties],
        assume=[_expr_to_str(e) for e in contract.assume],
        guarantee=[_expr_to_str(e) for e in contract.guarantee],
        governance=governance,
        violation_detect=contract.violation.detect if contract.violation else None,
        violation_action=contract.violation.action if contract.violation else None,
        lifecycle=_lower_lifecycle(contract.lifecycle),
        monitors=[_lower_monitor(m) for m in contract.monitors],
        norms=[_lower_norm(n, with_agents) for n in contract.norms] if with_norms else [],
        counts_as=[
            CountsAsSpecIR(when=r.when, creates=r.creates, if_=r.if_)
            for r in contract.counts_as
        ] if with_norms else [],
        enforced=enf is not None,
        enforcement_default=enf.default if enf else None,
        enforcement_point=(enf.point or DEFAULT_POINT) if enf else None,
        delegations=[
            DelegationSpecIR(
                norm=d.norm, to=list(d.to), grant=d.grant, revoke=d.revoke,
                max_depth=d.max_depth, until=d.until,
            )
            for d in contract.delegations
        ] if with_agents else [],
    )


def _lower_lifecycle(lc) -> LifecycleSpecIR | None:
    """Lower a LifecycleSpec AST node to LifecycleSpecIR."""
    if lc is None:
        return None
    return LifecycleSpecIR(
        states=list(lc.states),
        initial=lc.initial,
        terminal=list(lc.terminal),
        transitions=[
            LifecycleTransitionSpec(
                id=t.id,
                from_states=list(t.from_states),
                to_state=t.to_state,
                on=t.on,
                when=t.when,
                deadline_ms=t.deadline_ms,
                on_violation_transition=(
                    t.on_violation.transition if t.on_violation else None
                ),
                on_violation_severity=(
                    t.on_violation.severity if t.on_violation else None
                ),
                emit=list(t.emit),
            )
            for t in lc.transitions
        ],
    )


def _lower_monitor(m) -> MonitorSpecIR:
    """Lower a MonitorDef AST node to MonitorSpecIR."""
    return MonitorSpecIR(
        id=m.id,
        observe=list(m.observe),
        sampling_kind=m.sampling.kind if m.sampling else "event",
        sampling_period_ms=(
            m.sampling.period_ms if m.sampling else None
        ),
        rule=m.rule,
        on_match_violation=(m.on_match.violation if m.on_match else None),
        on_match_transition=(m.on_match.transition if m.on_match else None),
        on_match_severity=(m.on_match.severity if m.on_match else None),
    )


def _lower_norm(n, with_agents: bool = False) -> NormSpecIR:
    """Lower a NormDef AST node to NormSpecIR (Appendix F, F.9; G.13)."""
    od = n.on_denied if with_agents else None
    ov = n.on_violation
    eff = n.effects
    return NormSpecIR(
        id=n.id,
        kind=n.kind,
        bearer=n.bearer,
        counterparty=n.counterparty,
        trigger=n.trigger,
        until=n.until,
        content=n.content,
        deadline_ms=n.deadline_ms,
        on_violation_severity=(ov.severity if ov else None),
        on_violation_sanction=(ov.sanction if ov else None),
        on_violation_reparation=(ov.reparation if ov else None),
        on_violation_transition=(ov.transition if ov else None),
        effects_activate=list(eff.activate) if eff else [],
        effects_suspend=list(eff.suspend) if eff else [],
        effects_resume=list(eff.resume) if eff else [],
        effects_discharge=list(eff.discharge) if eff else [],
        on_amendment=n.on_amendment,
        transform_to=n.transform_to,
        on_denied_escalate_to=od.escalate_to if od else None,
        on_denied_timeout_ms=od.timeout_ms if od else None,
        on_denied_fallback=od.fallback if od else None,
        on_denied_notify=od.notify if od else None,
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
    from ..unparse import expr_to_source
    return expr_to_source(expr)