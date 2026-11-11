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
    AgentBinding,
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
    CountsAsDef,
    DelegationDef,
    DenialRecovery,
    DurationLiteral,
    EnforcementBlock,
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
    ModelBlock,
    ModelCommandDef,
    ModelParamDef,
    ModelPropertyDef,
    ModelRegimeMapDef,
    ModelRewardDef,
    ModelRewardItemDef,
    ModelUpdateDef,
    ModelVarDef,
    MetricDef,
    MonitorDef,
    MotivationBlock,
    NormDef,
    NormViolationSpec,
    OnMatchSpec,
    OnViolationSpec,
    ParallelStep,
    PowerEffects,
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
        value = float(items[0])
        if value in (float("inf"), float("-inf")):
            # 1e999: no finite value; an expression with it is not one
            raise ValueError(f"the number {items[0]} is too large")
        return FloatLiteral(value=value)

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

    def neg(self, items):
        # A negative number is a literal; any other operand x is read as
        # 0 - x, so that every consumer of BinaryOp handles it unchanged.
        operand = items[0]
        if isinstance(operand, IntLiteral):
            return IntLiteral(value=-operand.value)
        if isinstance(operand, FloatLiteral):
            return FloatLiteral(value=-operand.value)
        return BinaryOp(op="-", left=IntLiteral(value=0), right=operand)

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


_MEMBERSHIP_RE = re.compile(r"\s+IN\s*\[([^\[\]]*)\]")


def parse_rule(text: str) -> Expression:
    """Parse a SoS-DSL ``rule`` (Appendix E §E.3): a predicate that may
    also use set membership, ``x IN [a, b]``.

    The membership is read as ``x == __in__(a, b)``, so the result serves
    to check the syntax of a rule; it is not an input for the code
    generators, which translate the rule text themselves.
    """
    return parse_expr(_MEMBERSHIP_RE.sub(r" == __in__(\1)", text))


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
    invalid_autonomy = None
    try:
        autonomy = AutonomyLevel(autonomy_str)
    except ValueError:
        autonomy = AutonomyLevel.MEDIUM
        invalid_autonomy = autonomy_str

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
        invalid_autonomy=invalid_autonomy,
        # Agent governance extension (Appendix G); read whether or not
        # `agents: 0.1` is declared, as for the norms keys below.
        agent=_build_agent(data['agent']) if 'agent' in data else None,
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

    # === Norms extension (Appendix F) ===
    # Read whether or not `norms: 0.1` is declared; without the declaration
    # `cadl check` reports the keys as undeclared and does not check them,
    # and they are not lowered to the IR (F.11).
    norms_data = _get(data, 'norms')
    if isinstance(norms_data, list):
        contract.norms = [_build_norm(n) for n in norms_data if isinstance(n, dict)]
    ca_data = _get(data, 'counts_as')
    if isinstance(ca_data, list):
        contract.counts_as = [
            _build_counts_as(r) for r in ca_data if isinstance(r, dict)
        ]

    # === Agent governance extension (Appendix G) ===
    # Read whether or not `agents: 0.1` is declared; without the declaration
    # `cadl check` reports the keys as undeclared and does not check them,
    # and they are not lowered to the IR (G.14).
    if 'enforcement' in data:
        # The bare key (null) and an empty mapping mean "enforced with all
        # defaults" (G.3).
        contract.enforcement = _build_enforcement(data['enforcement'])
    if 'delegations' in data:
        del_data = data['delegations']
        if isinstance(del_data, list):
            contract.delegations = [
                _build_delegation(d) for d in del_data if isinstance(d, dict)
            ]
        elif del_data is not None:
            contract.invalid_delegations = _value_as_text(del_data)

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
    # YAML reads an unquoted true / false as a boolean; keep it as written.
    if isinstance(value, list):
        return [_id_str(v).strip() for v in value]
    return [_id_str(value).strip()]


def _opt_name(value) -> str | None:
    """An optional name (state, label) as written, or None when absent."""
    if value is None or value == '':
        return None
    return _id_str(value)


def _build_on_violation(data: dict) -> OnViolationSpec:
    return OnViolationSpec(
        transition=_opt_name(_get(data, 'transition')),
        severity=str(_get(data, 'severity', 'Major')),
    )


def _build_on_match(data: dict) -> OnMatchSpec:
    return OnMatchSpec(
        violation=_opt_name(_get(data, 'violation')),
        transition=_opt_name(_get(data, 'transition')),
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
    raw_deadline = _get(data, 'deadline')
    # YAML reads `deadline: yes` as a boolean; it is not a duration. A
    # negative number of seconds is not one either.
    deadline_ms = None if isinstance(raw_deadline, bool) else _duration_to_ms(raw_deadline)
    if deadline_ms is not None and deadline_ms < 0:
        deadline_ms = None
    return LifecycleTransition(
        id=_id_str(_get(data, 'id', '')),
        from_states=from_states,
        to_state=_id_str(_get(data, 'to', '')),
        on=str(_yaml_on_key(data, '')),
        when=(_get(data, 'when') or None),
        deadline_ms=deadline_ms,
        invalid_deadline=(
            _value_as_text(raw_deadline)
            if raw_deadline is not None and deadline_ms is None
            else None
        ),
        on_violation=on_viol,
        emit=emit,
    )


def _build_lifecycle(data: dict) -> LifecycleSpec:
    """Build a LifecycleSpec from a YAML mapping."""
    spec = LifecycleSpec(
        states=_as_str_list(_get(data, 'states', [])),
        initial=_opt_name(_get(data, 'initial')),
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
        # Mapping form (lenient, Appendix A §A.12): {kind: periodic,
        # period_ms: <non-negative integer>} or {kind: event}. Anything else
        # is read as event-driven and kept as text for `cadl check`.
        kind = str(_get(value, 'kind', 'event'))
        period = _get(value, 'period_ms')
        if (kind == 'periodic' and isinstance(period, int)
                and not isinstance(period, bool) and period >= 0):
            return SamplingSpec(kind='periodic', period_ms=period)
        if kind == 'event' and period is None:
            return SamplingSpec(kind='event')
        return SamplingSpec(kind='event', invalid=_value_as_text(value))
    s = str(value).strip()
    if s == 'event':
        return SamplingSpec(kind='event')
    m = _SAMPLING_PERIODIC_RE.match(s)
    if m:
        period = int(m.group(1))
        unit = m.group(2)
        period_ms = _duration_to_ms(f"{period}{unit}")
        return SamplingSpec(kind='periodic', period_ms=period_ms)
    # Unknown form: read as event-driven and keep the text for `cadl check`.
    return SamplingSpec(kind='event', invalid=_value_as_text(value))


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


# === Norms extension builders (Appendix F) ===

def _opt_text(value) -> str | None:
    """An optional event, content or condition as written, or None."""
    if value is None or value == '':
        return None
    return _value_as_text(value)


def _build_norm_violation(data: dict) -> NormViolationSpec:
    sanction = _get(data, 'sanction')
    return NormViolationSpec(
        severity=str(_get(data, 'severity', 'Major')),
        sanction=None if sanction is None else _value_as_text(sanction),
        reparation=_opt_name(_get(data, 'reparation')),
        transition=_opt_name(_get(data, 'transition')),
    )


def _build_power_effects(data: dict) -> PowerEffects:
    return PowerEffects(
        activate=_as_str_list(_get(data, 'activate')),
        suspend=_as_str_list(_get(data, 'suspend')),
        resume=_as_str_list(_get(data, 'resume')),
        discharge=_as_str_list(_get(data, 'discharge')),
    )


def _build_norm(data: dict) -> NormDef:
    """Build a NormDef from a YAML mapping (F.3)."""
    raw_deadline = _get(data, 'deadline')
    # As for lifecycle transitions: `yes` / `no` and negative numbers are
    # not durations; the text is kept for `cadl check`.
    deadline_ms = None if isinstance(raw_deadline, bool) else _duration_to_ms(raw_deadline)
    if deadline_ms is not None and deadline_ms < 0:
        deadline_ms = None

    on_viol_data = _get(data, 'on_violation')
    effects_data = _get(data, 'effects')

    # on_amendment: carry_over | discharge | { transform: X }
    raw_amend = _get(data, 'on_amendment')
    on_amendment, transform_to, invalid_amend = 'carry_over', None, None
    if isinstance(raw_amend, dict) and list(raw_amend) == ['transform']:
        on_amendment = 'transform'
        transform_to = _opt_name(raw_amend['transform'])
    elif raw_amend in ('carry_over', 'discharge'):
        on_amendment = raw_amend
    elif raw_amend is not None:
        on_amendment = invalid_amend = _value_as_text(raw_amend)

    return NormDef(
        id=_id_str(_get(data, 'id', '')),
        kind=_id_str(_get(data, 'kind', '')),
        bearer=_id_str(_get(data, 'bearer', '')).strip(),
        counterparty=_opt_name(_get(data, 'counterparty')),
        trigger=_opt_text(_get(data, 'trigger')),
        until=_opt_text(_get(data, 'until')),
        content=_opt_text(_get(data, 'content')) or '',
        deadline_ms=deadline_ms,
        invalid_deadline=(
            _value_as_text(raw_deadline)
            if raw_deadline is not None and deadline_ms is None
            else None
        ),
        on_violation=(
            _build_norm_violation(on_viol_data)
            if isinstance(on_viol_data, dict) else None
        ),
        # An `effects:` that is not a mapping has no effect, but is kept as
        # present so that `cadl check` can report it (N-5).
        effects=(
            _build_power_effects(effects_data)
            if isinstance(effects_data, dict)
            else (None if effects_data is None else PowerEffects())
        ),
        on_amendment=on_amendment,
        transform_to=transform_to,
        invalid_on_amendment=invalid_amend,
        on_denied=(
            _build_denial_recovery(data['on_denied']) if 'on_denied' in data else None
        ),
    )


def _build_counts_as(data: dict) -> CountsAsDef:
    """Build a CountsAsDef from a YAML mapping (F.3)."""
    return CountsAsDef(
        when=_opt_text(_get(data, 'when')) or '',
        creates=_id_str(_get(data, 'creates', '')),
        if_=_opt_text(_get(data, 'if')),
    )


# === Agent governance extension builders (Appendix G) ===

def _opt_ref(value) -> str | None:
    """An optional actor reference as written, or None when absent."""
    if value is None or value == '':
        return None
    return _id_str(value).strip()


def _build_agent(data) -> AgentBinding:
    """Build the `agent:` binding of an actor (G.3)."""
    if not isinstance(data, dict):
        return AgentBinding(invalid='null' if data is None else _value_as_text(data))
    kind = _get(data, 'kind')
    protocol = _get(data, 'protocol')
    binding = AgentBinding(
        kind='' if kind is None else _id_str(kind),
        protocol=None if protocol is None else _id_str(protocol),
        has_tools='tools' in data,
    )
    tools = _get(data, 'tools')
    if isinstance(tools, list):
        for item in tools:
            # YAML 1.1 reads an unquoted yes / no / on / off / true / false
            # as a boolean and null as None: these are not names (G-3).
            if isinstance(item, str):
                binding.tools.append(item)
            else:
                binding.tools_not_names.append(
                    'null' if item is None else _value_as_text(item))
    elif tools is not None:
        binding.invalid_tools = _value_as_text(tools)
    return binding


def _build_enforcement(data) -> EnforcementBlock:
    """Build `enforcement:`; null and {} are valid (G.3, G-5)."""
    if data is None:
        return EnforcementBlock()
    if not isinstance(data, dict):
        return EnforcementBlock(invalid=_value_as_text(data))
    default = _get(data, 'default')
    point = _get(data, 'point')
    return EnforcementBlock(
        default=None if default is None else _id_str(default),
        point=None if point is None else _id_str(point),
    )


def _build_denial_recovery(data) -> DenialRecovery:
    """Build `on_denied:` of a prohibition (G.3)."""
    if not isinstance(data, dict):
        return DenialRecovery(invalid='null' if data is None else _value_as_text(data))
    raw_timeout = _get(data, 'timeout')
    # As for `deadline:`: `yes` / `no` and negative numbers are not durations.
    timeout_ms = None if isinstance(raw_timeout, bool) else _duration_to_ms(raw_timeout)
    if timeout_ms is not None and timeout_ms < 0:
        timeout_ms = None
    return DenialRecovery(
        escalate_to=_opt_ref(_get(data, 'escalate_to')),
        timeout_ms=timeout_ms,
        invalid_timeout=(
            _value_as_text(raw_timeout)
            if raw_timeout is not None and timeout_ms is None else None
        ),
        fallback=_opt_text(_get(data, 'fallback')),
        notify=_opt_ref(_get(data, 'notify')),
    )


def _build_delegation(data: dict) -> DelegationDef:
    """Build an entry of `delegations:` (G.3)."""
    raw_depth = _get(data, 'max_depth')
    max_depth, invalid_depth = 1, None
    if raw_depth is not None:
        if isinstance(raw_depth, int) and not isinstance(raw_depth, bool) and raw_depth >= 1:
            max_depth = raw_depth
        else:
            invalid_depth = _value_as_text(raw_depth)
    to = _get(data, 'to')
    return DelegationDef(
        norm=_id_str(_get(data, 'norm', '')) if _get(data, 'norm') is not None else '',
        to=_as_str_list(to) if isinstance(to, list) else _parse_ref_texts(to),
        grant=_id_str(_get(data, 'grant')) if _get(data, 'grant') is not None else '',
        revoke=_opt_name(_get(data, 'revoke')),
        max_depth=max_depth,
        invalid_max_depth=invalid_depth,
        until=_opt_text(_get(data, 'until')),
        not_names=[
            k for k in ('norm', 'grant', 'revoke')
            if _get(data, k) is not None and not isinstance(_get(data, k), str)
        ],
    )


def _parse_ref_texts(value) -> list[str]:
    """`to: A` or `to: "[A, B[*]]"` as a list of actor references as written."""
    if value is None:
        return []
    from .unparse import actor_ref_to_source
    if isinstance(value, str):
        return [actor_ref_to_source(r) for r in _parse_actor_ref_list(value)]
    return [_id_str(value).strip()]


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
                else_steps = []
                else_data = data.get('else')
                if isinstance(else_data, list):
                    else_steps = [s for s in (_build_step(item) for item in else_data) if s is not None]
                return ConditionalStep(
                    condition=condition, then_steps=then_steps, else_steps=else_steps
                )

            if k_str == 'parallel':
                steps = []
                if isinstance(v, list):
                    steps = [s for s in (_build_step(item) for item in v) if s is not None]
                return ParallelStep(steps=steps)

            if k_str.startswith('barrier'):
                # "barrier: <predicate>"; an unquoted true / false arrives
                # from YAML as a boolean.
                cond_str = k_str[len('barrier'):].lstrip(':').strip()
                if not cond_str and v is not None:
                    cond_str = str(v).lower() if isinstance(v, bool) else str(v).strip()
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


def _value_as_text(value) -> str:
    """A YAML value rendered roughly as the user wrote it, for diagnostics."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (dict, list)):
        text = yaml.safe_dump(value, default_flow_style=True, sort_keys=False,
                              allow_unicode=True, width=1000).strip()
        return text[:-4].strip() if text.endswith("\n...") else text
    return str(value)


def _entry_as_text(item) -> str:
    """Render a YAML value roughly as it was written, for diagnostics."""
    if isinstance(item, dict) and len(item) == 1:
        (k, v), = item.items()
        return f"{k}: {v}"
    return str(item)


# === Stochastic model extension builders (Appendix H) ===
#
# The values are kept as the YAML loader read them; `cadl.stochastic`
# applies the rules of H.10 and parses the expression sub-languages.

def _model_entries(block: ModelBlock, data: dict, key: str, rule: str, build) -> list:
    """The entries of a list-valued key of `model:`, each built from a mapping."""
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        block.malformed.append((rule, f"model.{key} must be a sequence of mappings"))
        return []
    out = []
    for i, item in enumerate(value, start=1):
        if isinstance(item, dict):
            out.append(build(item))
        else:
            block.malformed.append(
                (rule, f"entry {i} of model.{key} is not a mapping: {_value_as_text(item)}"))
    return out


def _model_keys(data: dict) -> tuple:
    return tuple(k for k in data)


def _build_model_command(data: dict) -> ModelCommandDef:
    updates = data.get('updates')
    if isinstance(updates, list):
        updates = [
            ModelUpdateDef(prob=u.get('prob'), set=u.get('set'), keys=_model_keys(u))
            if isinstance(u, dict) else u
            for u in updates
        ]
    return ModelCommandDef(
        action=data.get('action'), when=data.get('when'), guard=data.get('guard'),
        updates=updates, keys=_model_keys(data),
    )


def _build_model_reward(data: dict) -> ModelRewardDef:
    items = data.get('items')
    if isinstance(items, list):
        items = [
            ModelRewardItemDef(action=i.get('action'), guard=i.get('guard'),
                               value=i.get('value'), keys=_model_keys(i))
            if isinstance(i, dict) else i
            for i in items
        ]
    return ModelRewardDef(id=data.get('id'), items=items, keys=_model_keys(data))


def _build_model(data) -> ModelBlock:
    """Build a ModelBlock from the value of `model:` (H.2)."""
    block = ModelBlock()
    if not isinstance(data, dict):
        block.malformed.append(("S-1", "model: must be a mapping"))
        return block
    block.keys = _model_keys(data)
    block.kind = data.get('kind')
    block.parameters = _model_entries(block, data, 'parameters', "S-2", lambda d: ModelParamDef(
        id=d.get('id'), source=d.get('from'), range=d.get('range'),
        value=d.get('value'), keys=_model_keys(d)))
    block.variables = _model_entries(block, data, 'variables', "S-1", lambda d: ModelVarDef(
        id=d.get('id'), type=d.get('type'), init=d.get('init'), keys=_model_keys(d)))
    block.commands = _model_entries(block, data, 'commands', "S-1", _build_model_command)
    block.labels = data.get('labels')
    block.regimes = data.get('regimes')
    block.rewards = _model_entries(block, data, 'rewards', "S-12", _build_model_reward)
    block.properties = _model_entries(block, data, 'properties', "S-13", lambda d: ModelPropertyDef(
        id=d.get('id'), regimes=d.get('regimes'), expr=d.get('expr'), keys=_model_keys(d)))
    block.regime_maps = _model_entries(block, data, 'regime_maps', "S-14", lambda d: ModelRegimeMapDef(
        id=d.get('id'), over=d.get('over'), regimes=d.get('regimes'),
        objective=d.get('objective'), constraint=d.get('constraint'),
        resolution=d.get('resolution'), keys=_model_keys(d)))
    return block


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
                env.raw[sk] = v
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
                    from_regime=_id_str(_get(t, 'from', '')),
                    to_regime=_id_str(_get(t, 'to', '')),
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

    # Stochastic model extension (Appendix H). Read whether or not
    # `stochastic: 0.1` is declared; without the declaration the block is
    # not interpreted (H.15).
    if 'model' in sos_data:
        sos.model = _build_model(sos_data['model'])

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
