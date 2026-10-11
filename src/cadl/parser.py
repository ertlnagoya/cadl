"""CADL Parser - converts CADL source text into AST nodes.

Uses a hybrid approach:
- YAML parser for the top-level structure (since CADL is YAML-like)
- Lark parser for expression sub-language (predicates, constraints)
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import yaml
from lark import Lark, Token, Transformer, v_args

from .ast_nodes import (
    ActorDef,
    ActorRef,
    AgentMotivationBlock,
    AlgorithmDef,
    AuthorityBlock,
    AutonomyLevel,
    BarrierStep,
    BinaryOp,
    BoolLiteral,
    CodegenSpec,
    Comprehension,
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
    GovernanceMotivationBlock,
    Identifier,
    IncentiveRule,
    IncentivesBlock,
    InformationBlock,
    IntLiteral,
    InterfaceDef,
    LifecycleSpec,
    LifecycleTransition,
    MemberAccess,
    MessageStep,
    MetricDef,
    MonitorDef,
    MotivationBlock,
    OnMatchSpec,
    OnViolationSpec,
    ParallelStep,
    ProtocolDef,
    QuantifiedExpr,
    RangeExpr,
    ResponsibilityGroup,
    RollbackBlock,
    SamplingSpec,
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
        obj = items[0]
        if not isinstance(obj, ActorRef):
            obj = ActorRef(name=str(obj))
        return MemberAccess(obj=obj, member=".".join(str(i) for i in items[1:]))

    def range_domain(self, items):
        end = int(items[1]) if str(items[1]).isdigit() else str(items[1])
        return RangeExpr(start=int(items[0]), end=end)

    def comprehension(self, items):
        return Comprehension(
            element=items[0], variable=str(items[1]), domain=items[2]
        )

    def function_call(self, items):
        name = str(items[0])
        args = items[1] if len(items) > 1 else []
        return FunctionCall(name=name, args=list(args) if not isinstance(args, list) else args)

    def arg_list(self, items):
        return list(items)

    def comparison(self, items):
        return BinaryOp(op=str(items[1]), left=items[0], right=items[2])

    def or_expr(self, items):
        result = items[0]
        for item in items[1:]:
            result = BinaryOp(op="OR", left=result, right=item)
        return result

    def and_expr(self, items):
        result = items[0]
        for item in items[1:]:
            result = BinaryOp(op="AND", left=result, right=item)
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
    id_str = _id_str(_get(data, 'id', ''))
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
            # A sharing entry is a quoted string "A -> B : item" whose item
            # is an identifier (Appendix A §A.4). Anything else is kept as
            # text for the type checker to report.
            m = None
            if isinstance(item, str):
                m = re.fullmatch(r'\s*(.+?)\s*->\s*(.+?)\s*:\s*([A-Za-z_][A-Za-z0-9_]*)\s*', item)
                if m is None:
                    # Lenient form: the item carries arguments or other text
                    # after its name. Keep the name and let the checker warn.
                    m = re.match(r'\s*(.+?)\s*->\s*(.+?)\s*:\s*([A-Za-z_][A-Za-z0-9_]*)(?![A-Za-z0-9_])', item)
                    if m:
                        info.lenient_sharing.append(item)
            if m:
                info.sharing.append(SharingDef(
                    source=_parse_actor_ref_str(m.group(1).strip()),
                    target=_parse_actor_ref_str(m.group(2).strip()),
                    data=m.group(3).strip(),
                ))
            else:
                info.invalid_sharing.append(_entry_as_text(item))

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
        id=_id_str(_get(data, 'id', '')),
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

    # === SoS-DSL extension (Appendix E) ===

    # lifecycle:
    lc_data = _get(data, 'lifecycle')
    if lc_data and isinstance(lc_data, dict):
        contract.lifecycle = _build_lifecycle(lc_data)

    # monitors:
    mon_data = _get(data, 'monitors')
    if isinstance(mon_data, list):
        contract.monitors = [
            _build_monitor(m) for m in mon_data if isinstance(m, dict)
        ]

    return contract


# === SoS-DSL extension builders (Appendix E) ===

_DURATION_RE = re.compile(r'^\s*(\d+)\s*(ms|s|min|h)\s*$')


def _duration_to_ms(value) -> int | None:
    """Normalize a duration literal (e.g. '5s', '500ms') to milliseconds.

    Accepts ints/floats (interpreted as seconds) and strings.
    Returns None if the value cannot be parsed.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(float(value) * 1000)
    s = str(value).strip()
    m = _DURATION_RE.match(s)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    if unit == 'ms':
        return n
    if unit == 's':
        return n * 1000
    if unit == 'min':
        return n * 60 * 1000
    if unit == 'h':
        return n * 60 * 60 * 1000
    return None


def _as_str_list(value) -> list[str]:
    """Coerce a YAML scalar / list to a list of stripped strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value]
    return [str(value).strip()]


def _build_on_violation(data: dict) -> OnViolationSpec:
    return OnViolationSpec(
        transition=(_get(data, 'transition') or None),
        severity=str(_get(data, 'severity', 'Major')),
    )


def _build_on_match(data: dict) -> OnMatchSpec:
    return OnMatchSpec(
        violation=(_get(data, 'violation') or None),
        transition=(_get(data, 'transition') or None),
        severity=str(_get(data, 'severity', 'Major')),
    )


def _yaml_on_key(data: dict, default=''):
    """Read the 'on' key, tolerating PyYAML's YAML-1.1 quirk that maps
    'on' to boolean True. We try the canonical string key first, then
    fall back to the boolean key produced by PyYAML.
    """
    if 'on' in data:
        return data['on']
    if True in data:
        return data[True]
    return default


def _build_lifecycle_transition(data: dict) -> LifecycleTransition:
    """Build a single lifecycle transition; normalizes from: to a list."""
    raw_from = _get(data, 'from')
    from_states = _as_str_list(raw_from)
    on_viol_data = _get(data, 'on_violation')
    on_viol = (
        _build_on_violation(on_viol_data)
        if isinstance(on_viol_data, dict)
        else None
    )
    emit = _as_str_list(_get(data, 'emit', []))
    return LifecycleTransition(
        id=_id_str(_get(data, 'id', '')),
        from_states=from_states,
        to_state=str(_get(data, 'to', '')),
        on=str(_yaml_on_key(data, '')),
        when=(_get(data, 'when') or None),
        deadline_ms=_duration_to_ms(_get(data, 'deadline')),
        on_violation=on_viol,
        emit=emit,
    )


def _build_lifecycle(data: dict) -> LifecycleSpec:
    """Build a LifecycleSpec from a YAML mapping."""
    spec = LifecycleSpec(
        states=_as_str_list(_get(data, 'states', [])),
        initial=(_get(data, 'initial') or None),
        terminal=_as_str_list(_get(data, 'terminal', [])),
    )
    trans_data = _get(data, 'transitions')
    if isinstance(trans_data, list):
        spec.transitions = [
            _build_lifecycle_transition(t)
            for t in trans_data
            if isinstance(t, dict)
        ]
    return spec


_SAMPLING_PERIODIC_RE = re.compile(
    r'^\s*periodic\s*\(\s*(\d+)\s*(ms|s|min|h)\s*\)\s*$'
)


def _build_sampling(value) -> SamplingSpec:
    """Parse a sampling spec string ('event' or 'periodic(500ms)')."""
    if value is None:
        return SamplingSpec(kind='event')
    if isinstance(value, dict):
        # Mapping form: {kind: ..., period_ms: ...}. As with the string
        # form, anything unrecognised is read as event-driven.
        kind = str(_get(value, 'kind', 'event'))
        period = _get(value, 'period_ms')
        if kind == 'periodic' and isinstance(period, int) and not isinstance(period, bool):
            return SamplingSpec(kind='periodic', period_ms=period)
        return SamplingSpec(kind='event')
    s = str(value).strip()
    if s == 'event':
        return SamplingSpec(kind='event')
    m = _SAMPLING_PERIODIC_RE.match(s)
    if m:
        period = int(m.group(1))
        unit = m.group(2)
        period_ms = _duration_to_ms(f"{period}{unit}")
        return SamplingSpec(kind='periodic', period_ms=period_ms)
    # Unknown form -> treat as event-driven, preserve raw on rule via sampling.kind
    return SamplingSpec(kind='event')


def _build_monitor(data: dict) -> MonitorDef:
    """Build a MonitorDef from a YAML mapping."""
    on_match_data = _get(data, 'on_match')
    on_match = (
        _build_on_match(on_match_data)
        if isinstance(on_match_data, dict)
        else None
    )
    return MonitorDef(
        id=_id_str(_get(data, 'id', '')),
        observe=_as_str_list(_get(data, 'observe', [])),
        sampling=_build_sampling(_get(data, 'sampling')),
        rule=str(_get(data, 'rule', '')),
        on_match=on_match,
    )


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
        id=_id_str(_get(data, 'id', '')),
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


def _parse_bound(value) -> Optional[int]:
    """Read a verification `bound:` (a step count). Non-integers are dropped."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _id_str(value) -> str:
    """An id as written. YAML reads an unquoted true / false as a boolean."""
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _entry_as_text(item) -> str:
    """Render a YAML value roughly as it was written, for diagnostics."""
    if isinstance(item, dict) and len(item) == 1:
        (k, v), = item.items()
        return f"{k}: {v}"
    return str(item)


def _build_extensions(data) -> list:
    """Read `extensions:` as (name, version) pairs (Appendix A §A.2)."""
    out = []
    if isinstance(data, dict):
        data = [{k: v} for k, v in data.items()]
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                for k, v in item.items():
                    out.append((str(k), "" if v is None else str(v)))
            elif item is not None:
                out.append((str(item), ""))
    return out


def _build_motivation(data) -> MotivationBlock | None:
    """Read the `motivation:` block of Appendix C, keeping it verbatim.

    The block must never make a file invalid (Appendix C), so a value that
    cannot be interpreted is left at its default; it stays in ``raw``.
    """
    if data is None:
        return None
    block = MotivationBlock(raw=data)
    if not isinstance(data, dict):
        return block
    agent = _get(data, 'agent')
    if isinstance(agent, dict):
        a = AgentMotivationBlock()
        if _get(agent, 'profile') is not None:
            a.profile = str(_get(agent, 'profile'))
        values = _get(agent, 'values')
        if isinstance(values, list):
            a.values = [float(v) for v in values if _is_finite_number(v)]
        block.agent = a
    gov = _get(data, 'governance')
    if isinstance(gov, dict):
        g = GovernanceMotivationBlock()
        if _get(gov, 'model') is not None:
            g.model = str(_get(gov, 'model'))
        for key, cast in (('rho', float), ('kappa', float), ('budget_base', int), ('wait_scale', float)):
            v = _get(gov, key)
            if _is_finite_number(v):
                try:
                    setattr(g, key, cast(v))
                except (ValueError, OverflowError):
                    pass
        block.governance = g
    return block


def _is_finite_number(v) -> bool:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:
        return False


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
                sk = str(k)
                sv = str(v)
                # Preserve quoted strings from YAML as-is (avoid
                # expression parsing of e.g. "unity-mcp-custom")
                if isinstance(v, str):
                    env.entries[sk] = StringLiteral(value=sv)
                else:
                    try:
                        env.entries[sk] = parse_expr(sv)
                    except Exception:
                        env.entries[sk] = StringLiteral(value=sv)
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
                    id=_id_str(_get(m, 'id', '')),
                    formula=_get(m, 'formula'),
                    target=_get(m, 'target'),
                ))

    # Verifications
    verify_data = _get(sos_data, 'verification', [])
    if isinstance(verify_data, list):
        for v in verify_data:
            if isinstance(v, dict):
                method = _get(v, 'method')
                expr = _get(v, 'expr')
                sos.verifications.append(VerificationSpec(
                    id=str(_get(v, 'id', '')),
                    type=str(_get(v, 'type', '')),
                    target=_get(v, 'target'),
                    property=_get(v, 'property'),
                    method=str(method) if method is not None else None,
                    # YAML reads an unquoted true / false as a boolean.
                    expr=(str(expr).lower() if isinstance(expr, bool) else str(expr))
                    if expr is not None else None,
                    bound=_parse_bound(_get(v, 'bound')),
                ))

    # Codegen
    codegen_data = _get(sos_data, 'codegen', [])
    if isinstance(codegen_data, list):
        for cg in codegen_data:
            if isinstance(cg, dict):
                sos.codegen.append(CodegenSpec(
                    # `target:` defaults to python (Appendix A §A.9)
                    target=str(_get(cg, 'target') or 'python'),
                    output=_get(cg, 'output'),
                    mappings=_get(cg, 'mappings', {}) or {},
                ))

    # Extension declarations and the motivation block (Appendices A.2, C)
    sos.extensions = _build_extensions(_get(sos_data, 'extensions'))
    sos.motivation = _build_motivation(_get(sos_data, 'motivation'))

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
