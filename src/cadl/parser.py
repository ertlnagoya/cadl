"""CADL Parser - converts CADL source text into AST nodes.

Uses a hybrid approach:
- YAML parser for the top-level structure (since CADL is YAML-like)
- Lark parser for expression sub-language (predicates, constraints)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml
from lark import Lark, Token, Transformer, v_args

from .ast_nodes import (
    ActorDef,
    ActorRef,
    AlgorithmDef,
    AuthorityBlock,
    AutonomyLevel,
    BarrierStep,
    BinaryOp,
    BoolLiteral,
    CodegenSpec,
    ComputeStep,
    ConditionalStep,
    ContextBlock,
    ContractDef,
    DurationLiteral,
    EnvironmentDef,
    Expression,
    FallbackBlock,
    FloatLiteral,
    FunctionCall,
    Identifier,
    IncentiveRule,
    IncentivesBlock,
    InformationBlock,
    InterfaceDef,
    IntLiteral,
    MemberAccess,
    MessageStep,
    MetricDef,
    ParallelStep,
    ProtocolDef,
    QuantifiedExpr,
    RangeExpr,
    ResponsibilityGroup,
    RollbackBlock,
    SharingDef,
    SoSDefinition,
    SoSType,
    StringLiteral,
    TimingBlock,
    TransitionDef,
    UnaryOp,
    VerificationSpec,
    ViewDef,
    ViolationBlock,
)

GRAMMAR_PATH = Path(__file__).parent / "grammar.lark"


class CADLParseError(Exception):
    """Raised when CADL parsing fails."""
    pass


# === Expression parser (Lark-based) ===

class ExprTransformer(Transformer):
    """Transform Lark expression parse tree into AST nodes."""

    def float_lit(self, items):
        return FloatLiteral(value=float(items[0]))

    def int_lit(self, items):
        return IntLiteral(value=int(items[0]))

    def bool_lit(self, items):
        return BoolLiteral(value=str(items[0]) == "true")

    def string_lit(self, items):
        s = str(items[0])
        if s.startswith('"') and s.endswith('"'):
            s = s[1:-1]
        return StringLiteral(value=s)

    def duration_lit(self, items):
        s = str(items[0])
        m = re.match(r"(\d+)(ms|s|min|h)", s)
        if m:
            return DurationLiteral(value=int(m.group(1)), unit=m.group(2))
        return DurationLiteral(value=0, unit="s")

    def actor_ref_simple(self, items):
        return ActorRef(name=str(items[0]))

    def actor_ref_wild(self, items):
        return ActorRef(name=str(items[0]), index="*")

    def actor_ref_range(self, items):
        name = str(items[0])
        start = int(items[1])
        end = int(items[2]) if str(items[2]).isdigit() else str(items[2])
        return ActorRef(name=name, index=RangeExpr(start=start, end=end))

    def actor_ref_index(self, items):
        return ActorRef(name=str(items[0]), index=items[1])

    def actor_ref_expr(self, items):
        return items[0]

    def member_access(self, items):
        parts = [str(i) for i in items]
        obj = ActorRef(name=parts[0])
        return MemberAccess(obj=obj, member=".".join(parts[1:]))

    def function_call(self, items):
        name = str(items[0])
        args = items[1] if len(items) > 1 else []
        return FunctionCall(name=name, args=list(args) if not isinstance(args, list) else args)

    def arg_list(self, items):
        return list(items)

    def comparison(self, items):
        return BinaryOp(op=str(items[1]), left=items[0], right=items[2])

    def or_expr_inner(self, items):
        if len(items) == 1:
            return items[0]
        result = items[0]
        for i in range(1, len(items)):
            result = BinaryOp(op="OR", left=result, right=items[i])
        return result

    def not_expr(self, items):
        return UnaryOp(op="NOT", operand=items[0])

    def arith_expr(self, items):
        if len(items) == 1:
            return items[0]
        result = items[0]
        i = 1
        while i < len(items):
            op = str(items[i])
            right = items[i + 1]
            result = BinaryOp(op=op, left=result, right=right)
            i += 2
        return result

    def term_expr(self, items):
        if len(items) == 1:
            return items[0]
        result = items[0]
        i = 1
        while i < len(items):
            op = str(items[i])
            right = items[i + 1]
            result = BinaryOp(op=op, left=result, right=right)
            i += 2
        return result

    def grouped(self, items):
        return items[0]

    def quantified_expr(self, items):
        quantifier = "for_all" if "all" in str(items[0]) else "exists"
        var = str(items[1])
        domain = items[2]
        pred = items[3]
        return QuantifiedExpr(quantifier=quantifier, variable=var, domain=domain, predicate=pred)

    def start(self, items):
        return items[0]


_expr_parser: Optional[Lark] = None


def _get_expr_parser() -> Lark:
    global _expr_parser
    if _expr_parser is None:
        grammar_text = GRAMMAR_PATH.read_text()
        _expr_parser = Lark(grammar_text, parser="earley", maybe_placeholders=False)
    return _expr_parser


def parse_expr(text: str) -> Expression:
    """Parse a CADL expression string into an AST Expression node."""
    parser = _get_expr_parser()
    tree = parser.parse(text)
    return ExprTransformer().transform(tree)


# === YAML-based structural parser ===

def _get(data: dict, key: str, default=None):
    """Safely get a value from a dict."""
    if not isinstance(data, dict):
        return default
    return data.get(key, default)


def _parse_actor_ref_str(s: str) -> ActorRef:
    """Parse an actor reference string like 'ROBOT[*]', 'TAXI[1..N]', 'CENTRAL'."""
    s = s.strip()
    m = re.match(r'^([a-zA-Z_]\w*)\[(\*)\]$', s)
    if m:
        return ActorRef(name=m.group(1), index="*")

    m = re.match(r'^([a-zA-Z_]\w*)\[(\d+)\.\.(\w+)\]$', s)
    if m:
        start = int(m.group(2))
        end_str = m.group(3)
        end: Union[int, str] = int(end_str) if end_str.isdigit() else end_str
        return ActorRef(name=m.group(1), index=RangeExpr(start=start, end=end))

    m = re.match(r'^([a-zA-Z_]\w*)\[(\w+)\]$', s)
    if m:
        idx = m.group(2)
        return ActorRef(name=m.group(1), index=idx)

    return ActorRef(name=s)


def _parse_actor_ref_list(data) -> List[ActorRef]:
    """Parse a list of actor references."""
    if isinstance(data, list):
        return [_parse_actor_ref_str(str(item)) for item in data]
    if isinstance(data, str):
        # Handle "[A, B[*], C]" format
        s = data.strip()
        if s.startswith('[') and s.endswith(']'):
            s = s[1:-1]
        elif s.startswith('{') and s.endswith('}'):
            s = s[1:-1]
        parts = [p.strip() for p in s.split(',')]
        return [_parse_actor_ref_str(p) for p in parts if p]
    return []


def _parse_predicate(item) -> Expression:
    """Parse a predicate item - either a string (quoted) or an expression."""
    if isinstance(item, str):
        # Try to parse as expression first
        try:
            return parse_expr(item)
        except Exception:
            return StringLiteral(value=item)
    return StringLiteral(value=str(item))


def _build_actor(data: dict) -> ActorDef:
    """Build an ActorDef from a YAML dict."""
    id_str = str(_get(data, 'id', ''))
    actor_ref = _parse_actor_ref_str(id_str)

    role = str(_get(data, 'role', ''))
    autonomy_str = str(_get(data, 'autonomy', 'medium'))
    try:
        autonomy = AutonomyLevel(autonomy_str)
    except ValueError:
        autonomy = AutonomyLevel.MEDIUM

    capabilities = _get(data, 'capabilities', []) or []

    interface = None
    iface_data = _get(data, 'interface')
    if iface_data and isinstance(iface_data, dict):
        interface = InterfaceDef(
            inputs=_get(iface_data, 'input', []) or [],
            outputs=_get(iface_data, 'output', []) or [],
        )

    return ActorDef(
        id=actor_ref,
        role=role,
        autonomy=autonomy,
        capabilities=[str(c) for c in capabilities],
        interface=interface,
    )


def _build_authority(data: dict) -> AuthorityBlock:
    """Build an AuthorityBlock from a YAML dict."""
    auth = AuthorityBlock()
    auth.decision_scope = _get(data, 'decision_scope')
    holder = _get(data, 'decision_holder')
    if holder:
        auth.decision_holder = _parse_actor_ref_str(str(holder))
    beta = _get(data, 'beta')
    if beta is not None:
        auth.beta = float(beta)
    auth.mode = _get(data, 'mode')
    return auth


def _build_information(data: dict) -> InformationBlock:
    """Build an InformationBlock from a YAML dict."""
    info = InformationBlock()
    alpha = _get(data, 'alpha')
    if alpha is not None:
        info.alpha = float(alpha)

    views_data = _get(data, 'views', {})
    if isinstance(views_data, dict):
        for k, v in views_data.items():
            info.views.append(ViewDef(actor=_parse_actor_ref_str(str(k)), view=str(v)))

    sharing_data = _get(data, 'sharing', [])
    if isinstance(sharing_data, list):
        for item in sharing_data:
            if isinstance(item, str):
                # Parse "A -> B : data" format
                m = re.match(r'(.+?)\s*->\s*(.+?)\s*:\s*(\w+)', item)
                if m:
                    info.sharing.append(SharingDef(
                        source=_parse_actor_ref_str(m.group(1).strip()),
                        target=_parse_actor_ref_str(m.group(2).strip()),
                        data=m.group(3).strip(),
                    ))

    return info


def _build_responsibilities(data: dict) -> List[ResponsibilityGroup]:
    """Build responsibility groups from YAML dict."""
    groups = []
    if isinstance(data, dict):
        for k, v in data.items():
            actor = _parse_actor_ref_str(str(k))
            items = []
            if isinstance(v, list):
                items = [str(i) for i in v]
            elif isinstance(v, str):
                items = [v]
            groups.append(ResponsibilityGroup(actor=actor, items=items))
    return groups


def _build_incentives(data: dict) -> IncentivesBlock:
    """Build an IncentivesBlock from a YAML dict."""
    inc = IncentivesBlock()
    inc.type = _get(data, 'type')
    lam = _get(data, 'lambda')
    if lam is not None:
        inc.lambda_ = float(lam)
    rules_data = _get(data, 'rules', [])
    if isinstance(rules_data, list):
        inc.rules = [IncentiveRule(description=str(r)) for r in rules_data]
    return inc


def _build_violation(data: dict) -> ViolationBlock:
    """Build a ViolationBlock from a YAML dict."""
    return ViolationBlock(
        detect=_get(data, 'detect'),
        action=_get(data, 'action'),
        escalation=_get(data, 'escalation'),
    )


def _build_contract(data: dict) -> ContractDef:
    """Build a ContractDef from a YAML dict."""
    contract = ContractDef(
        id=str(_get(data, 'id', '')),
        parties=_parse_actor_ref_list(_get(data, 'parties', [])),
    )

    # assume / guarantee
    assume_data = _get(data, 'assume', [])
    if isinstance(assume_data, list):
        contract.assume = [_parse_predicate(p) for p in assume_data]

    guarantee_data = _get(data, 'guarantee', [])
    if isinstance(guarantee_data, list):
        contract.guarantee = [_parse_predicate(p) for p in guarantee_data]

    # authority
    auth_data = _get(data, 'authority')
    if auth_data and isinstance(auth_data, dict):
        contract.authority = _build_authority(auth_data)

    # information
    info_data = _get(data, 'information')
    if info_data and isinstance(info_data, dict):
        contract.information = _build_information(info_data)

    # responsibilities
    resp_data = _get(data, 'responsibilities')
    if resp_data and isinstance(resp_data, dict):
        contract.responsibilities = _build_responsibilities(resp_data)

    # incentives
    inc_data = _get(data, 'incentives')
    if inc_data and isinstance(inc_data, dict):
        contract.incentives = _build_incentives(inc_data)

    # violation
    vio_data = _get(data, 'violation')
    if vio_data and isinstance(vio_data, dict):
        contract.violation = _build_violation(vio_data)

    # duration
    dur = _get(data, 'duration')
    if dur is not None:
        contract.duration = str(dur)

    return contract


def _build_step(data) -> Optional[Any]:
    """Build a protocol step from a YAML entry."""
    if isinstance(data, str):
        s = data.strip()
        # Message step: "A -> B : msg"
        m = re.match(r'(.+?)\s*->\s*(.+?)\s*:\s*(.+)', s)
        if m:
            sender = _parse_actor_ref_str(m.group(1).strip())
            receiver = _parse_actor_ref_str(m.group(2).strip())
            msg_str = m.group(3).strip()
            # Try to parse message as function call or identifier
            try:
                msg = parse_expr(msg_str)
            except Exception:
                msg = Identifier(name=msg_str)
            return MessageStep(sender=sender, receiver=receiver, message=msg)

        # Compute step: "A : computation"
        m = re.match(r'(.+?)\s*:\s*(.+)', s)
        if m:
            actor = _parse_actor_ref_str(m.group(1).strip())
            comp_str = m.group(2).strip()
            try:
                comp = parse_expr(comp_str)
            except Exception:
                comp = Identifier(name=comp_str)
            return ComputeStep(actor=actor, computation=comp)

        # Just an identifier or expression
        return ComputeStep(actor=ActorRef(name=""), computation=Identifier(name=s))

    if isinstance(data, dict):
        for k, v in data.items():
            k_str = str(k).strip()
            v_str = str(v).strip() if v is not None else ""

            # Message step: YAML parsed "A -> B : msg" as {"A -> B": "msg"}
            m = re.match(r'(.+?)\s*->\s*(.+)', k_str)
            if m:
                sender = _parse_actor_ref_str(m.group(1).strip())
                receiver = _parse_actor_ref_str(m.group(2).strip())
                try:
                    msg = parse_expr(v_str)
                except Exception:
                    msg = Identifier(name=v_str)
                return MessageStep(sender=sender, receiver=receiver, message=msg)

            # Compute step: YAML parsed "A : computation" as {"A": "computation"}
            # (only if key looks like an actor ref and doesn't match special keys)
            if k_str.startswith('if '):
                cond_str = k_str[3:].rstrip(':')
                try:
                    condition = parse_expr(cond_str)
                except Exception:
                    condition = StringLiteral(value=cond_str)
                then_steps = []
                if isinstance(v, list):
                    then_steps = [s for s in (_build_step(item) for item in v) if s is not None]
                return ConditionalStep(condition=condition, then_steps=then_steps)

            if k_str == 'parallel':
                steps = []
                if isinstance(v, list):
                    steps = [s for s in (_build_step(item) for item in v) if s is not None]
                return ParallelStep(steps=steps)

            if k_str.startswith('barrier'):
                cond_str = k_str.replace('barrier:', '').strip()
                if not cond_str and isinstance(v, str):
                    cond_str = v
                try:
                    condition = parse_expr(cond_str)
                except Exception:
                    condition = StringLiteral(value=cond_str)
                return BarrierStep(condition=condition)

            # Generic compute step from dict entry
            if re.match(r'^[a-zA-Z_]', k_str):
                actor = _parse_actor_ref_str(k_str)
                try:
                    comp = parse_expr(v_str)
                except Exception:
                    comp = Identifier(name=v_str)
                return ComputeStep(actor=actor, computation=comp)

    return None


def _build_protocol(data: dict) -> ProtocolDef:
    """Build a ProtocolDef from a YAML dict."""
    proto = ProtocolDef(
        id=str(_get(data, 'id', '')),
        trigger=str(_get(data, 'trigger', '')),
    )

    proto.precondition = _get(data, 'precondition')
    proto.postcondition = _get(data, 'postcondition')
    proto.safety_invariant = _get(data, 'safety_invariant')

    steps_data = _get(data, 'steps', [])
    if isinstance(steps_data, list):
        proto.steps = [s for s in (_build_step(item) for item in steps_data) if s is not None]

    timing_data = _get(data, 'timing')
    if timing_data and isinstance(timing_data, dict):
        proto.timing = TimingBlock(entries={str(k): str(v) for k, v in timing_data.items()})

    fallback_data = _get(data, 'fallback')
    if fallback_data and isinstance(fallback_data, dict):
        proto.fallback = FallbackBlock(entries={str(k): str(v) for k, v in fallback_data.items()})

    rollback_data = _get(data, 'rollback')
    if rollback_data and isinstance(rollback_data, dict):
        proto.rollback = RollbackBlock(
            condition=_get(rollback_data, 'condition'),
            action=_get(rollback_data, 'action'),
        )

    return proto


def _build_sos(data: dict) -> SoSDefinition:
    """Build a SoSDefinition from the top-level YAML dict."""
    sos_data = _get(data, 'sos')
    if sos_data is None:
        raise CADLParseError("Missing 'sos:' top-level key")
    if not isinstance(sos_data, dict):
        raise CADLParseError("'sos:' must be a mapping")

    sos = SoSDefinition(name=str(_get(sos_data, 'name', '')))

    # Type
    type_str = _get(sos_data, 'type')
    if type_str:
        try:
            sos.type = SoSType(str(type_str))
        except ValueError:
            raise CADLParseError(f"Invalid SoS type: '{type_str}'. Must be one of: Directed, Acknowledged, Collaborative, Virtual")

    # Version
    ver = _get(sos_data, 'version')
    if ver is not None:
        sos.version = str(ver)

    # Description
    desc = _get(sos_data, 'description')
    if desc is not None:
        sos.description = str(desc)

    # Context
    ctx_data = _get(sos_data, 'context')
    if ctx_data and isinstance(ctx_data, dict):
        ctx = ContextBlock()
        env_data = _get(ctx_data, 'environment')
        if env_data and isinstance(env_data, dict):
            env = EnvironmentDef()
            for k, v in env_data.items():
                try:
                    env.entries[str(k)] = parse_expr(str(v))
                except Exception:
                    env.entries[str(k)] = StringLiteral(value=str(v))
            ctx.environment = env
        assumptions = _get(ctx_data, 'assumptions', [])
        if isinstance(assumptions, list):
            ctx.assumptions = [str(a) for a in assumptions]
        sos.context = ctx

    # Actors
    actors_data = _get(sos_data, 'actors', [])
    if isinstance(actors_data, list):
        sos.actors = [_build_actor(a) for a in actors_data if isinstance(a, dict)]

    # Contracts
    contracts_data = _get(sos_data, 'contracts', [])
    if isinstance(contracts_data, list):
        sos.contracts = [_build_contract(c) for c in contracts_data if isinstance(c, dict)]

    # Protocols
    protocols_data = _get(sos_data, 'protocols', [])
    if isinstance(protocols_data, list):
        sos.protocols = [_build_protocol(p) for p in protocols_data if isinstance(p, dict)]

    # Algorithms
    algos_data = _get(sos_data, 'algorithms')
    if algos_data and isinstance(algos_data, dict):
        for name, opts in algos_data.items():
            algo = AlgorithmDef(name=str(name))
            if isinstance(opts, dict):
                algo.central = _get(opts, 'central')
                algo.local = _get(opts, 'local')
            sos.algorithms.append(algo)

    # Transitions
    trans_data = _get(sos_data, 'transitions', [])
    if isinstance(trans_data, list):
        for t in trans_data:
            if isinstance(t, dict):
                sos.transitions.append(TransitionDef(
                    from_regime=str(_get(t, 'from', '')),
                    to_regime=str(_get(t, 'to', '')),
                    condition=_get(t, 'condition'),
                    protocol=_get(t, 'protocol'),
                    safety_invariant=_get(t, 'safety_invariant'),
                ))

    # Metrics
    metrics_data = _get(sos_data, 'metrics', [])
    if isinstance(metrics_data, list):
        for m in metrics_data:
            if isinstance(m, dict):
                sos.metrics.append(MetricDef(
                    id=str(_get(m, 'id', '')),
                    formula=_get(m, 'formula'),
                    target=_get(m, 'target'),
                ))

    # Verifications
    verify_data = _get(sos_data, 'verification', [])
    if isinstance(verify_data, list):
        for v in verify_data:
            if isinstance(v, dict):
                sos.verifications.append(VerificationSpec(
                    id=str(_get(v, 'id', '')),
                    type=str(_get(v, 'type', '')),
                    target=_get(v, 'target'),
                    property=_get(v, 'property'),
                ))

    # Codegen
    codegen_data = _get(sos_data, 'codegen', [])
    if isinstance(codegen_data, list):
        for cg in codegen_data:
            if isinstance(cg, dict):
                sos.codegen.append(CodegenSpec(
                    target=str(_get(cg, 'target', 'python')),
                    output=_get(cg, 'output'),
                    mappings=_get(cg, 'mappings', {}) or {},
                ))

    return sos


def parse(source: str) -> SoSDefinition:
    """Parse CADL source text and return an AST SoSDefinition node."""
    try:
        data = yaml.safe_load(source)
    except yaml.YAMLError as e:
        raise CADLParseError(f"YAML parse error: {e}") from e

    if not isinstance(data, dict):
        raise CADLParseError("CADL file must contain a YAML mapping at the top level")

    return _build_sos(data)


def parse_file(path: Union[str, Path]) -> SoSDefinition:
    """Parse a CADL file and return an AST SoSDefinition node."""
    source = Path(path).read_text(encoding="utf-8")
    return parse(source)
