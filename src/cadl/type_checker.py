"""CADL Type Checker - validates semantic constraints on the AST.

Checks performed (per spec §6.2.2):
1. Actor reference existence - all referenced actors are defined
2. Contract party validation - no duplicates, all parties exist
3. Protocol step sender/receiver match actor definitions
4. Information sharing declarations name declared actors (comparing
   them with the actual message exchanges is planned, not implemented)
5. Institutional parameter range constraints (0 <= alpha, beta, lambda <= 1)
6. SoS-DSL static semantics, rules L-1 to M-3 (Appendix E §E.4)
7. Norms static semantics, rules N-1 to N-12 (Appendix F §F.7), when the
   file declares `norms: 0.1`
8. Agent governance static semantics, rules G-1 to G-18 (Appendix G §G.9),
   when the file declares `agents: 0.1` (which requires `norms: 0.1`)
9. Stochastic model static semantics, rules S-1 to S-15 (Appendix H §H.10),
   when the file declares `stochastic: 0.1`
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field

from .ast_nodes import (
    ActorDef,
    ActorRef,
    AuthorityBlock,
    BarrierStep,
    BinaryOp,
    Comprehension,
    ComputeStep,
    ConditionalStep,
    ContractDef,
    DurationLiteral,
    Expression,
    FunctionCall,
    IncentivesBlock,
    InformationBlock,
    MemberAccess,
    MessageStep,
    NormDef,
    ParallelStep,
    QuantifiedExpr,
    ProtocolDef,
    RangeExpr,
    ResponsibilityGroup,
    SharingDef,
    SoSDefinition,
    SourceLocation,
    Step,
    UnaryOp,
    ViewDef,
)


# Appendix A §A.1: identifier = ( letter | "_" ) , { letter | digit | "_" }
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Appendix A §A.11 and Appendix E §E.3 (the same set as the expression grammar)
RESERVED_WORDS = frozenset({"AND", "OR", "NOT", "IN", "true", "false", "in", "exists"})
# Targets `cadl codegen` emits, and the simulator configs of `cadl sim-gen`
CODEGEN_TARGETS = ("python", "solidity", "opa", "unity-csharp",
                   "agent-rego", "agent-cedar")
SIM_TARGETS = ("unity", "go")
# Extensions this processor implements, with the versions it knows
KNOWN_EXTENSIONS = {"sos-dsl": ("0.1",), "norms": ("0.1",), "agents": ("0.1",),
                    "stochastic": ("0.1",)}


@dataclass
class TypeError:
    """A type checking error."""
    message: str
    loc: SourceLocation | None = None
    severity: str = "error"  # "error" or "warning"

    def __str__(self) -> str:
        loc_str = ""
        if self.loc:
            loc_str = f" at line {self.loc.line}, col {self.loc.column}"
        return f"[{self.severity.upper()}]{loc_str}: {self.message}"


@dataclass
class TypeCheckResult:
    """Result of type checking."""
    errors: list[TypeError] = field(default_factory=list)
    warnings: list[TypeError] = field(default_factory=list)
    # Informational diagnostics: they never make a file invalid.
    infos: list[TypeError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    def add_error(self, message: str, loc: SourceLocation | None = None) -> None:
        self.errors.append(TypeError(message=message, loc=loc, severity="error"))

    def add_warning(self, message: str, loc: SourceLocation | None = None) -> None:
        self.warnings.append(TypeError(message=message, loc=loc, severity="warning"))

    def add_info(self, message: str, loc: SourceLocation | None = None) -> None:
        self.infos.append(TypeError(message=message, loc=loc, severity="info"))


class TypeChecker:
    """Performs semantic validation on a CADL AST."""

    def __init__(self) -> None:
        self.result = TypeCheckResult()
        self.actor_names: set[str] = set()
        self.sos_actors: list = []
        self.contract_ids: set[str] = set()
        self.protocol_ids: set[str] = set()
        self.norms_declared = False
        # Agent governance extension: interpreted only when `agents: 0.1`
        # is declared together with `norms: 0.1` (G-1).
        self.agents_active = False
        self.tool_servers: dict[str, list[str]] = {}
        self.agent_kinds: dict[str, str] = {}

    def check(self, sos: SoSDefinition) -> TypeCheckResult:
        """Run all type checks on the SoS definition."""
        from .norms import norms_declared
        self.result = TypeCheckResult()
        self.norms_declared = norms_declared(sos)
        self._check_agents_declaration(sos)
        self._collect_definitions(sos)
        self._check_actors(sos)
        if self.agents_active:
            self._check_agent_actors(sos)
        self._check_contracts(sos)
        self._check_protocols(sos)
        self._check_transitions(sos)
        self._check_algorithms(sos)
        self._check_metrics(sos)
        self._check_codegen(sos)
        self._check_extensions(sos)
        if self.norms_declared:
            self._check_temporal_elsewhere(sos)
        if self.agents_active:
            self._check_ungoverned_tool_servers(sos)
        self._check_stochastic_model(sos)
        return self.result

    # --- Stochastic model extension (Appendix H, H.10) ---

    def _check_stochastic_model(self, sos: SoSDefinition) -> None:
        """Rules S-1 to S-15 for `model:`, when the file declares
        `stochastic: 0.1`; otherwise one warning (H.15)."""
        from .stochastic import analyse, stochastic_declared
        if sos.model is None:
            return
        if not stochastic_declared(sos):
            self.result.add_warning(
                "The file uses model:, which belongs to the stochastic extension, "
                "but does not declare 'stochastic: 0.1' under extensions:; it is an "
                "undeclared extension key, and is neither checked, lowered to the IR "
                "nor exported (Appendix H, H.15)",
                sos.loc,
            )
            return
        analysis = analyse(sos)
        for message in analysis.infos:
            self.result.add_info(message, sos.loc)
        for message in analysis.warnings:
            self.result.add_warning(message, sos.loc)
        for message in analysis.errors:
            self.result.add_error(message, sos.loc)

    def _collect_definitions(self, sos: SoSDefinition) -> None:
        """Collect all defined names for reference checking."""
        for actor in sos.actors:
            self.actor_names.add(actor.id.name)
            self._check_identifier(actor.id.name, "Actor ID", actor.loc)
        for contract in sos.contracts:
            self._check_identifier(contract.id, "Contract ID", contract.loc)
            if contract.id in self.contract_ids:
                self.result.add_error(
                    f"Duplicate contract ID: '{contract.id}'",
                    contract.loc,
                )
            self.contract_ids.add(contract.id)
        for protocol in sos.protocols:
            self._check_identifier(protocol.id, "Protocol ID", protocol.loc)
            if protocol.id in self.protocol_ids:
                self.result.add_error(
                    f"Duplicate protocol ID: '{protocol.id}'",
                    protocol.loc,
                )
            self.protocol_ids.add(protocol.id)

    def _check_actors(self, sos: SoSDefinition) -> None:
        """Check actor definitions for validity."""
        seen_names: set[str] = set()
        for actor in sos.actors:
            name = actor.id.name
            if name in seen_names:
                self.result.add_error(
                    f"Duplicate actor ID: '{name}'",
                    actor.loc,
                )
            seen_names.add(name)

            if actor.invalid_autonomy is not None:
                self.result.add_error(
                    f"Unknown autonomy '{actor.invalid_autonomy}' for actor "
                    f"'{name}'; expected one of low, medium, high",
                    actor.loc,
                )

            if not actor.role:
                self.result.add_warning(
                    f"Actor '{name}' has no role defined",
                    actor.loc,
                )

    # --- SoS-DSL extension (Appendix E, E.4) ---

    _MESSAGE_EVENT_RE = re.compile(r"^(.+?)\s*->\s*(.+?)\s*:\s*(.+)$")

    def _check_rule(self, text: str, where: str, loc) -> None:
        """A rule that is not a predicate of Appendix A / E.3 is kept as
        text by the generators; say so instead of passing it silently."""
        from .parser import parse_rule
        try:
            parse_rule(text)
        except Exception:
            self.result.add_warning(
                f"{where} is not a valid rule (Appendix E.3) and is kept as "
                f"text: {text}",
                loc,
            )

    def _check_event(self, text: str, where: str, loc) -> None:
        """`on:` holds a message event (A -> B : msg) or a rule."""
        from .parser import _parse_actor_ref_str, parse_expr
        m = self._MESSAGE_EVENT_RE.match(text.strip())
        if "->" in text and m:
            for part, role in ((m.group(1), "sender"), (m.group(2), "receiver")):
                self._check_actor_ref(
                    _parse_actor_ref_str(part.strip()), f"{where} ({role})"
                )
            try:
                parse_expr(m.group(3).strip())
            except Exception:
                self.result.add_warning(
                    f"{where} names a message that is not a function call or "
                    f"an identifier: {m.group(3).strip()}",
                    loc,
                )
        else:
            self._check_rule(text, where, loc)

    def _check_lifecycle(self, contract: ContractDef, ctx: str) -> None:
        lc = contract.lifecycle
        if lc is None:
            return
        states = set(lc.states)
        for state in lc.states:
            self._check_identifier(state, f"Lifecycle state of {ctx}", lc.loc)

        # L-1: initial and terminal states are declared states.
        if lc.initial is None:
            self.result.add_error(f"Lifecycle of {ctx} has no initial state", lc.loc)
        elif lc.initial not in states:
            self.result.add_error(
                f"Lifecycle initial state '{lc.initial}' of {ctx} is not listed "
                f"in states (L-1)", lc.loc,
            )
        for state in lc.terminal:
            if state not in states:
                self.result.add_error(
                    f"Lifecycle terminal state '{state}' of {ctx} is not listed "
                    f"in states (L-1)", lc.loc,
                )

        seen_ids: set[str] = set()
        for tr in lc.transitions:
            where = f"lifecycle transition '{tr.id}' of {ctx}"
            self._check_identifier(tr.id, f"Lifecycle transition ID of {ctx}", tr.loc)
            if tr.id and tr.id in seen_ids:
                self.result.add_error(f"Duplicate {where}", tr.loc)
            seen_ids.add(tr.id)

            # L-2: from and to are declared states.
            if not tr.from_states:
                self.result.add_error(f"The {where} has no 'from' state (L-2)", tr.loc)
            for state in tr.from_states:
                if state not in states:
                    self.result.add_error(
                        f"The {where} leaves '{state}', which is not listed in "
                        f"states (L-2)", tr.loc,
                    )
            if tr.to_state not in states:
                self.result.add_error(
                    f"The {where} enters '{tr.to_state}', which is not listed in "
                    f"states (L-2)", tr.loc,
                )

            # E.3: a deadline is a duration literal (an integer with ms, s,
            # min or h). One that cannot be read would be lost from the IR.
            if tr.invalid_deadline is not None:
                self.result.add_error(
                    f"The {where} has deadline '{tr.invalid_deadline}', which is "
                    f"not a duration: write an integer followed by ms, s, min or h "
                    f"(for example 5s)",
                    tr.loc,
                )

            # L-4: a deadline should say what happens when it is missed.
            if tr.deadline_ms is not None and tr.on_violation is None:
                self.result.add_warning(
                    f"The {where} has a deadline but no on_violation block (L-4)",
                    tr.loc,
                )

            # L-5: on_violation.transition names a declared state.
            target = tr.on_violation.transition if tr.on_violation else None
            if target is not None and target not in states:
                self.result.add_error(
                    f"on_violation.transition '{target}' of the {where} is not a "
                    f"state listed in states (L-5); it names the target state, "
                    f"not a transition id", tr.loc,
                )

            if tr.on:
                self._check_event(tr.on, f"'on' of the {where}", tr.loc)
            if tr.when:
                self._check_rule(tr.when, f"'when' of the {where}", tr.loc)

        # L-3: a terminal state exists and one can be reached.
        if not lc.terminal:
            self.result.add_error(
                f"Lifecycle of {ctx} lists no terminal state (L-3)", lc.loc
            )
        elif lc.initial in states and not (
            self._reachable_states(contract) & set(lc.terminal)
        ):
            self.result.add_error(
                f"No terminal state of the lifecycle of {ctx} is reachable from "
                f"the initial state '{lc.initial}' (L-3)", lc.loc,
            )

    @staticmethod
    def _reachable_states(contract: ContractDef) -> set[str]:
        """States an instance can be in: those reached by transitions and
        deadline violations, and the targets of monitors, which force a
        move from whatever state the instance is in."""
        lc = contract.lifecycle
        edges: dict[str, set[str]] = {}
        for tr in lc.transitions:
            targets = {tr.to_state}
            if tr.on_violation and tr.on_violation.transition:
                targets.add(tr.on_violation.transition)
            for state in tr.from_states:
                edges.setdefault(state, set()).update(targets)
        forced = {
            mon.on_match.transition for mon in contract.monitors
            if mon.on_match and mon.on_match.transition
        }
        reached = {lc.initial} | forced
        frontier = list(reached)
        while frontier:
            for nxt in edges.get(frontier.pop(), ()):
                if nxt not in reached:
                    reached.add(nxt)
                    frontier.append(nxt)
        return reached

    _OBSERVE_RESERVED = ("time", "state")

    def _check_monitors(self, contract: ContractDef, ctx: str) -> None:
        from .parser import parse_expr
        states = set(contract.lifecycle.states) if contract.lifecycle else set()
        seen_ids: set[str] = set()
        for mon in contract.monitors:
            where = f"monitor '{mon.id}' of {ctx}"
            self._check_identifier(mon.id, f"Monitor ID of {ctx}", mon.loc)
            if mon.id and mon.id in seen_ids:
                self.result.add_error(f"Duplicate {where}", mon.loc)
            seen_ids.add(mon.id)

            # E.3: sampling is `event` or `periodic(<duration>)`.
            if mon.sampling is not None and mon.sampling.invalid is not None:
                self.result.add_error(
                    f"The {where} has sampling '{mon.sampling.invalid}', which is "
                    f"neither 'event' nor 'periodic(<duration>)' (for example "
                    f"periodic(500ms)); the mapping form needs "
                    f"{{kind: periodic, period_ms: <non-negative integer>}}",
                    mon.loc,
                )

            # M-1: an observation is an attribute of a declared actor, a
            # message name, or one of the reserved identifiers. A bare
            # identifier cannot be told from a message name and is accepted.
            for entry in mon.observe:
                if entry in self._OBSERVE_RESERVED:
                    continue
                try:
                    expr = parse_expr(entry)
                except Exception:
                    expr = None
                if isinstance(expr, MemberAccess):
                    if expr.obj.name not in self.actor_names:
                        self.result.add_error(
                            f"The {where} observes '{entry}', but "
                            f"'{expr.obj.name}' is not a declared actor (M-1)",
                            mon.loc,
                        )
                elif not (isinstance(expr, ActorRef) and expr.index is None):
                    self.result.add_error(
                        f"The {where} observes '{entry}', which is neither an "
                        f"attribute of an actor nor a name (M-1)",
                        mon.loc,
                    )

            if mon.rule:
                self._check_rule(mon.rule, f"'rule' of the {where}", mon.loc)
            else:
                self.result.add_error(f"The {where} has no rule", mon.loc)

            if mon.on_match:
                # M-2: the label is an identifier; it need not be declared.
                if mon.on_match.violation:
                    self._check_identifier(
                        str(mon.on_match.violation),
                        f"Violation label of the {where}", mon.loc,
                    )
                # M-3: on_match.transition names a state of this contract.
                target = mon.on_match.transition
                if target is not None and target not in states:
                    self.result.add_error(
                        f"on_match.transition '{target}' of the {where} is not a "
                        f"state of the lifecycle of this contract (M-3); it names "
                        f"the target state, not a transition id", mon.loc,
                    )

    # --- Norms extension (Appendix F, F.7) ---

    _TEMPORAL_SIGNATURES = {
        "within": "within(d, p)",
        "throughout": "throughout(d, p)",
        "until": "until(p, q, d)",
    }

    def _check_temporal_call(self, call: FunctionCall, where: str, loc) -> None:
        """N-10: the arguments of F.4, with a duration literal greater than zero."""
        from .norms import TEMPORAL_OPERATORS
        arity, dpos = TEMPORAL_OPERATORS[call.name]
        sig = self._TEMPORAL_SIGNATURES[call.name]
        if len(call.args) != arity:
            self.result.add_error(
                f"{where}: '{call.name}' takes {arity} arguments, {sig}, but is "
                f"given {len(call.args)} (N-10)", loc,
            )
            return
        duration = call.args[dpos]
        if not isinstance(duration, DurationLiteral):
            self.result.add_error(
                f"{where}: the duration of '{call.name}' must be a duration "
                f"literal such as 300s, as in {sig} (N-10)", loc,
            )
        elif duration.value <= 0:
            self.result.add_error(
                f"{where}: the duration of '{call.name}' must be greater than "
                f"zero, got {duration.value}{duration.unit} (N-10)", loc,
            )
        for i, arg in enumerate(call.args):
            if i != dpos and isinstance(arg, Comprehension):
                self.result.add_error(
                    f"{where}: argument {i + 1} of '{call.name}' must be a "
                    f"predicate, as in {sig} (N-10)", loc,
                )

    def _check_tpredicate(self, text: str, where: str, loc) -> None:
        """A predicate in which the temporal operators of F.4 may appear
        (the content of a norm). As in a rule, `x IN [a, b]` is allowed (F.3)."""
        from .norms import has_temporal_text, temporal_calls
        from .parser import parse_rule
        try:
            expr = parse_rule(text)
        except Exception:
            if has_temporal_text(text):
                self.result.add_error(
                    f"{where} uses a temporal operator but is not a predicate "
                    f"(Appendix A.10); write within(d, p), throughout(d, p) or "
                    f"until(p, q, d) with a duration literal d (N-10): {text}",
                    loc,
                )
            else:
                self.result.add_warning(
                    f"{where} is not a predicate (Appendix A.10) and is kept as "
                    f"text: {text}", loc,
                )
            return
        for call in temporal_calls(expr):
            self._check_temporal_call(call, where, loc)

    def _check_no_temporal(self, text, where: str, loc, as_rule: bool = True) -> None:
        """N-10: no temporal operator outside the places F.4 allows (the
        assume / guarantee items of a contract and the content of a norm).
        `text` is a predicate as written or an already parsed expression."""
        from .ast_nodes import StringLiteral
        from .norms import has_temporal_text, temporal_calls
        from .parser import parse_expr, parse_rule
        if not isinstance(text, str):
            expr = text
            text = expr.value if isinstance(expr, StringLiteral) else None
            if text is None:
                from .unparse import expr_to_source
                text = expr_to_source(expr)
                found = next(temporal_calls(expr), None) is not None
            else:
                found = has_temporal_text(text)
        else:
            try:
                expr = (parse_rule if as_rule else parse_expr)(text)
                found = next(temporal_calls(expr), None) is not None
            except Exception:
                found = has_temporal_text(text)
        if found:
            self.result.add_error(
                f"{where} uses a temporal operator (within, throughout, until), "
                f"which norms 0.1 allows only in assume:, guarantee: and the "
                f"content: of a norm (N-10): {text}", loc,
            )

    def _check_temporal_placement(self, contract: ContractDef, ctx: str) -> None:
        """N-10 for the predicates of the core contract and of Appendix E."""
        for key, items in (("assume", contract.assume), ("guarantee", contract.guarantee)):
            for expr in items:
                self._check_contract_item_temporal(expr, f"An item of {key} of {ctx}", contract.loc)
        if contract.lifecycle:
            for tr in contract.lifecycle.transitions:
                where = f"lifecycle transition '{tr.id}' of {ctx}"
                if tr.on and not ("->" in tr.on and self._MESSAGE_EVENT_RE.match(tr.on.strip())):
                    self._check_no_temporal(tr.on, f"'on' of the {where}", tr.loc, True)
                if tr.when:
                    self._check_no_temporal(tr.when, f"'when' of the {where}", tr.loc, True)
        for mon in contract.monitors:
            if mon.rule:
                self._check_no_temporal(
                    mon.rule, f"'rule' of the monitor '{mon.id}' of {ctx}", mon.loc, True
                )

    def _check_temporal_elsewhere(self, sos: SoSDefinition) -> None:
        """N-10 for the predicates outside contracts: transition conditions
        and safety invariants, metric formulas, verification expressions and
        protocol conditions (F.4)."""
        for t in sos.transitions:
            where = f"transition {t.from_regime}->{t.to_regime}"
            for key in ("condition", "safety_invariant"):
                text = getattr(t, key)
                if isinstance(text, str) and text:
                    self._check_no_temporal(text, f"'{key}' of the {where}", t.loc, False)
        for m in sos.metrics:
            if isinstance(m.formula, str) and m.formula:
                self._check_no_temporal(m.formula, f"'formula' of metric '{m.id}'", m.loc, False)
        for v in sos.verifications:
            if v.expr:
                self._check_no_temporal(v.expr, f"'expr' of verification '{v.id}'", v.loc, False)
        for proto in sos.protocols:
            ctx = f"protocol '{proto.id}'"
            for key in ("precondition", "postcondition", "safety_invariant"):
                text = getattr(proto, key)
                if isinstance(text, str) and text:
                    self._check_no_temporal(text, f"'{key}' of {ctx}", proto.loc, False)
            if proto.rollback and isinstance(proto.rollback.condition, str) and proto.rollback.condition:
                self._check_no_temporal(
                    proto.rollback.condition, f"'rollback.condition' of {ctx}", proto.loc, False
                )
            self._check_steps_no_temporal(proto.steps, ctx, proto.loc)

    def _check_steps_no_temporal(self, steps, ctx: str, loc) -> None:
        for step in steps:
            if isinstance(step, ConditionalStep):
                self._check_no_temporal(step.condition, f"a step condition of {ctx}", loc)
                self._check_steps_no_temporal(step.then_steps, ctx, loc)
                self._check_steps_no_temporal(step.else_steps, ctx, loc)
            elif isinstance(step, BarrierStep):
                self._check_no_temporal(step.condition, f"a barrier condition of {ctx}", loc)
            elif isinstance(step, ParallelStep):
                self._check_steps_no_temporal(step.steps, ctx, loc)

    def _check_contract_item_temporal(self, expr: Expression, where: str, loc) -> None:
        from .ast_nodes import StringLiteral
        from .norms import has_temporal_text, temporal_calls
        if isinstance(expr, StringLiteral):
            # Not a predicate of A.10: kept as an opaque item.
            if has_temporal_text(expr.value):
                self.result.add_error(
                    f"{where} uses a temporal operator but is not a predicate "
                    f"(Appendix A.10); write within(d, p), throughout(d, p) or "
                    f"until(p, q, d) with a duration literal d (N-10): {expr.value}",
                    loc,
                )
            return
        for call in temporal_calls(expr):
            self._check_temporal_call(call, where, loc)

    def _norm_term_kind(self, text: str, events: set[str]) -> str:
        """How a trigger, until, content or when is read (F.3, N-8):
        "event", "message" or "predicate"."""
        t = text.strip()
        if t in events and _IDENTIFIER_RE.fullmatch(t) and t not in RESERVED_WORDS:
            return "event"
        if "->" in t and self._MESSAGE_EVENT_RE.match(t):
            return "message"
        return "predicate"

    def _check_norm_message(self, text: str, where: str, loc) -> str:
        """N-9: the sender and the receiver of a message event are declared
        actors. Returns the sender as written in canonical form."""
        from .parser import _parse_actor_ref_str, parse_expr
        from .unparse import actor_ref_to_source
        m = self._MESSAGE_EVENT_RE.match(text.strip())
        sender = _parse_actor_ref_str(m.group(1).strip())
        receiver = _parse_actor_ref_str(m.group(2).strip())
        for ref, role in ((sender, "sender"), (receiver, "receiver")):
            if ref.name not in self.actor_names:
                self.result.add_error(
                    f"The {role} '{actor_ref_to_source(ref)}' of the message event "
                    f"in {where} is not a declared actor (N-9)", loc,
                )
        try:
            parse_expr(m.group(3).strip())
        except Exception:
            self.result.add_warning(
                f"{where} names a message that is not a function call or "
                f"an identifier: {m.group(3).strip()}", loc,
            )
        return actor_ref_to_source(sender)

    def _check_norm_term(
        self, text: str, where: str, loc, events: set[str], norm_ids: set[str],
        temporal: bool = False,
    ) -> tuple[str, str | None]:
        """Check a trigger, until, content or when. Returns its kind and,
        for a message event, the sender. Temporal operators are allowed only
        when `temporal` is set (the content of a norm, F.4)."""
        kind = self._norm_term_kind(text, events)
        if kind == "message":
            return kind, self._check_norm_message(text, where, loc)
        if kind == "predicate":
            t = text.strip()
            # N-8: a whole identifier that is not an institutional event but
            # the id of a norm is probably a mistake.
            if t in norm_ids:
                self.result.add_warning(
                    f"{where} is '{t}', the id of a norm; it is read as a state "
                    f"variable, since it is not an institutional event of a "
                    f"counts_as: rule (N-8)", loc,
                )
            if temporal:
                self._check_tpredicate(text, where, loc)
            else:
                self._check_predicate_text(text, where, loc)
        return kind, None

    def _check_predicate_text(self, text: str, where: str, loc) -> None:
        """A predicate of a norm event or a counts_as rule: no temporal
        operator (N-10); text that is not a predicate is kept as text."""
        from .parser import parse_rule
        self._check_no_temporal(text, where, loc)
        try:
            parse_rule(text)
        except Exception:
            self.result.add_warning(
                f"{where} is not a predicate (Appendix A.10) and is kept as "
                f"text: {text}", loc,
            )

    def _check_norms(self, contract: ContractDef, ctx: str) -> None:
        from .norms import NORM_KINDS
        from .parser import _parse_actor_ref_str
        from .unparse import actor_ref_to_source

        norms_by_id: dict[str, NormDef] = {}
        norm_ids = {n.id for n in contract.norms if n.id}
        events = {r.creates for r in contract.counts_as if r.creates}
        # Events that some rule creates from a predicate rather than a message
        message_events_only = {
            r.creates for r in contract.counts_as
            if r.creates and self._norm_term_kind(r.when, set()) == "message"
        } - {
            r.creates for r in contract.counts_as
            if r.creates and self._norm_term_kind(r.when, set()) != "message"
        }
        states = set(contract.lifecycle.states) if contract.lifecycle else set()
        party_names = {p.name for p in contract.parties}

        # N-1: norm ids are identifiers, unique within the contract.
        for norm in contract.norms:
            if not norm.id:
                self.result.add_error(f"A norm of {ctx} has no id (N-1)", norm.loc)
                continue
            self._check_identifier(norm.id, f"Norm ID of {ctx}", norm.loc, rule="N-1")
            if norm.id in norms_by_id:
                self.result.add_error(f"Duplicate norm '{norm.id}' of {ctx} (N-1)", norm.loc)
            else:
                norms_by_id[norm.id] = norm

        # counts_as: rules; N-1 for the institutional event names.
        for rule in contract.counts_as:
            name = rule.creates
            where = f"the counts_as rule for '{name}' of {ctx}" if name else f"a counts_as rule of {ctx}"
            if not name:
                self.result.add_error(
                    f"A counts_as rule of {ctx} names no institutional event "
                    f"(creates:) (N-1)", rule.loc,
                )
            else:
                self._check_identifier(
                    name, f"Institutional event of {ctx}", rule.loc, rule="N-1"
                )
                if name in norm_ids:
                    self.result.add_error(
                        f"Institutional event '{name}' of {ctx} has the same name as "
                        f"a norm of the contract (N-1)", rule.loc,
                    )
            if not rule.when:
                self.result.add_error(f"{where[0].upper()}{where[1:]} has no 'when' (F.3)", rule.loc)
            else:
                # `when:` is a message event or a predicate, never an event name;
                # N-8 concerns trigger, until and content only.
                self._check_norm_term(rule.when, f"'when' of {where}", rule.loc, set(), set())
            if rule.if_:
                self._check_predicate_text(rule.if_, f"'if' of {where}", rule.loc)

        for norm in contract.norms:
            where = f"norm '{norm.id}' of {ctx}"
            kind = norm.kind
            # N-2: the kind is one of the four.
            if kind not in NORM_KINDS:
                self.result.add_error(
                    f"The {where} has kind '{kind}'; expected one of "
                    f"{', '.join(NORM_KINDS)} (N-2)" if kind else
                    f"The {where} has no kind; expected one of "
                    f"{', '.join(NORM_KINDS)} (N-2)", norm.loc,
                )

            # N-3: bearer and counterparty are declared actors, and should
            # be parties of the contract.
            if not norm.bearer:
                self.result.add_error(f"The {where} has no bearer (N-3)", norm.loc)
            for role, text in (("bearer", norm.bearer), ("counterparty", norm.counterparty)):
                if not text:
                    continue
                ref = _parse_actor_ref_str(text)
                if ref.name not in self.actor_names:
                    self.result.add_error(
                        f"The {role} '{text}' of the {where} is not a declared "
                        f"actor (N-3)", norm.loc,
                    )
                elif ref.index == "*" or isinstance(ref.index, RangeExpr):
                    # One actor per norm instance: no wildcard, no range.
                    self.result.add_error(
                        f"The {role} '{text}' of the {where} is a wildcard or a "
                        f"range; name one actor, without an index or with an "
                        f"index the contract instance binds, such as "
                        f"{ref.name}[i] (N-3)", norm.loc,
                    )
                elif ref.name not in party_names:
                    self.result.add_warning(
                        f"The {role} '{text}' of the {where} is not a party of "
                        f"the contract (N-3)", norm.loc,
                    )

            # N-4: a deadline is a duration, and deadline and on_violation
            # belong to obligations and prohibitions.
            if norm.invalid_deadline is not None:
                self.result.add_error(
                    f"The {where} has deadline '{norm.invalid_deadline}', which is "
                    f"not a duration: write an integer followed by ms, s, min or h "
                    f"(for example 5s) (N-4)", norm.loc,
                )
            if kind in ("permission", "power"):
                if norm.deadline_ms is not None or norm.invalid_deadline is not None:
                    self.result.add_error(
                        f"The {where} is a {kind} and cannot have a deadline (N-4)",
                        norm.loc,
                    )
                if norm.on_violation is not None:
                    self.result.add_error(
                        f"The {where} is a {kind} and cannot have on_violation (N-4)",
                        norm.loc,
                    )

            # N-5: effects belong to powers; a power has at least one.
            if norm.effects is not None and kind in NORM_KINDS and kind != "power":
                self.result.add_error(
                    f"The {where} is a {kind}; only a power can have effects (N-5)",
                    norm.loc,
                )
            if kind == "power":
                eff = norm.effects
                targets = (
                    eff.activate + eff.suspend + eff.resume + eff.discharge
                    if eff else []
                )
                if not targets:
                    self.result.add_error(
                        f"The {where} is a power without effects; give at least one "
                        f"of activate, suspend, resume, discharge (N-5)", norm.loc,
                    )
            if norm.effects is not None:
                eff = norm.effects
                for effect, names in (("activate", eff.activate), ("suspend", eff.suspend),
                                      ("resume", eff.resume), ("discharge", eff.discharge)):
                    for name in names:
                        if name == norm.id:
                            self.result.add_error(
                                f"effects.{effect} of the {where} names the power "
                                f"itself (N-5)", norm.loc,
                            )
                        elif name not in norm_ids:
                            self.result.add_error(
                                f"effects.{effect} of the {where} names '{name}', "
                                f"which is not a norm of {ctx} (N-5)", norm.loc,
                            )

            if norm.on_violation is not None:
                ov = norm.on_violation
                # N-6: a reparation is an obligation or prohibition without
                # a trigger of its own.
                if ov.reparation is not None:
                    rep = norms_by_id.get(ov.reparation)
                    if rep is None:
                        self.result.add_error(
                            f"on_violation.reparation '{ov.reparation}' of the {where} "
                            f"is not a norm of {ctx} (N-6)", norm.loc,
                        )
                    else:
                        if rep.kind not in ("obligation", "prohibition"):
                            self.result.add_error(
                                f"on_violation.reparation '{ov.reparation}' of the "
                                f"{where} is a {rep.kind or 'norm without kind'}; it must "
                                f"be an obligation or a prohibition (N-6)", norm.loc,
                            )
                        if rep.trigger is not None:
                            self.result.add_error(
                                f"on_violation.reparation '{ov.reparation}' of the "
                                f"{where} has a trigger; a reparation is activated "
                                f"only by the violation (N-6)", norm.loc,
                            )
                # N-7: the transition names a lifecycle state.
                if ov.transition is not None:
                    if contract.lifecycle is None:
                        self.result.add_error(
                            f"on_violation.transition '{ov.transition}' of the {where} "
                            f"needs a lifecycle: in {ctx} (N-7)", norm.loc,
                        )
                    elif ov.transition not in states:
                        self.result.add_error(
                            f"on_violation.transition '{ov.transition}' of the {where} "
                            f"is not a state listed in lifecycle.states (N-7)", norm.loc,
                        )

            # Events and content (N-8, N-9, N-10).
            for key in ("trigger", "until"):
                text = getattr(norm, key)
                if text is not None:
                    self._check_norm_term(text, f"'{key}' of the {where}", norm.loc,
                                          events, norm_ids)
            if not norm.content:
                self.result.add_error(f"The {where} has no content (F.3)", norm.loc)
            else:
                term, sender = self._check_norm_term(
                    norm.content, f"'content' of the {where}", norm.loc, events,
                    norm_ids, temporal=True,
                )
                bearer = (
                    actor_ref_to_source(_parse_actor_ref_str(norm.bearer))
                    if norm.bearer else ""
                )
                # N-9: a power is exercised by an institutional event created
                # from a message event, or by a message its bearer sends.
                if kind == "power":
                    if term == "predicate":
                        self.result.add_error(
                            f"The content of the {where} must be an institutional "
                            f"event of {ctx} or a message event sent by its bearer, "
                            f"not '{norm.content}' (N-9)", norm.loc,
                        )
                    elif term == "event" and norm.content.strip() not in message_events_only:
                        self.result.add_error(
                            f"The content of the {where} is the institutional event "
                            f"'{norm.content.strip()}', which a counts_as rule creates "
                            f"from a predicate; a power is exercised by an event "
                            f"created from a message event (N-9)", norm.loc,
                        )
                    elif term == "message" and sender != bearer:
                        self.result.add_error(
                            f"The content of the {where} is a message sent by "
                            f"'{sender}', not by its bearer '{norm.bearer}' (N-9)",
                            norm.loc,
                        )
                # N-9 (SHOULD): the bearer sends the message it is bound to send
                # or not to send.
                elif (kind in ("obligation", "prohibition") and term == "message"
                      and bearer and sender != bearer):
                    self.result.add_warning(
                        f"The content of the {where} is a message sent by "
                        f"'{sender}', not by its bearer '{norm.bearer}' (N-9)",
                        norm.loc,
                    )

            # N-11: transform names an identifier; other values are not of F.3.
            if norm.invalid_on_amendment is not None:
                self.result.add_error(
                    f"The {where} has on_amendment '{norm.invalid_on_amendment}'; "
                    f"expected carry_over, discharge or {{transform: <norm id>}} (F.3)",
                    norm.loc,
                )
            elif norm.on_amendment == "transform":
                if norm.transform_to is None:
                    self.result.add_error(
                        f"on_amendment.transform of the {where} names no norm (N-11)",
                        norm.loc,
                    )
                else:
                    self._check_identifier(
                        norm.transform_to, f"on_amendment.transform of the {where}",
                        norm.loc, rule="N-11",
                    )

        # N-6: following reparation from a norm never leads back to it.
        reported: set[frozenset] = set()
        for start in norms_by_id:
            path = [start]
            cur = start
            while True:
                ov = norms_by_id[cur].on_violation
                nxt = ov.reparation if ov else None
                if nxt is None or nxt not in norms_by_id:
                    break
                if nxt == start:
                    cycle = frozenset(path)
                    if cycle not in reported:
                        reported.add(cycle)
                        self.result.add_error(
                            f"The reparations of {ctx} form a cycle: "
                            f"{' -> '.join(path + [start])} (N-6)",
                            norms_by_id[start].loc,
                        )
                    break
                if nxt in path:
                    break  # a cycle that does not pass through start
                path.append(nxt)
                cur = nxt

        # N-12: a permission and a prohibition of the same bearer and content.
        for perm in contract.norms:
            if perm.kind != "permission":
                continue
            for proh in contract.norms:
                if (proh.kind == "prohibition" and perm.bearer == proh.bearer
                        and perm.content.strip() == proh.content.strip()):
                    self.result.add_warning(
                        f"The permission '{perm.id}' and the prohibition "
                        f"'{proh.id}' of {ctx} have the same bearer and content, so "
                        f"the two conflict (N-12)",
                        perm.loc,
                    )

    # --- Agent governance extension (Appendix G, G.9) ---

    _YAML_NAME_NOTE = (
        "a YAML 1.1 loader reads an unquoted yes, no, on, off, true, false "
        "or null as a value, not as a name"
    )

    def _check_agents_declaration(self, sos: SoSDefinition) -> None:
        """G-1, and the undeclared use of the keys (G.14)."""
        from .agents import agent_keys_used, agents_declared
        declared = agents_declared(sos)
        self.agents_active = declared and self.norms_declared
        self.tool_servers = {}
        if declared and not self.norms_declared:
            self.result.add_error(
                "The file declares 'agents: 0.1' without 'norms: 0.1', which the "
                "agent governance extension requires; agent:, enforcement:, "
                "delegations: and on_denied: are not interpreted (G-1)",
                sos.loc,
            )
        elif not declared:
            keys = agent_keys_used(sos)
            if keys:
                self.result.add_warning(
                    f"The file uses {', '.join(keys)}, which belong{'s' if len(keys) == 1 else ''} "
                    f"to the agent governance extension, but does not declare "
                    f"'agents: 0.1' under extensions:; they are undeclared extension "
                    f"keys, and are neither checked nor lowered to the IR "
                    f"(Appendix G, G.14)",
                    sos.loc,
                )
        if self.agents_active:
            self.tool_servers = {
                a.id.name: list(a.agent.tools) for a in sos.actors
                if a.agent is not None and a.agent.kind == "tool_server"
            }

    def _check_agent_actors(self, sos: SoSDefinition) -> None:
        """G-2 and G-3: the `agent:` binding of an actor."""
        from .agents import AGENT_KINDS, is_hyphen_name
        self.sos_actors = list(sos.actors)
        self.agent_kinds = {
            a.id.name: a.agent.kind for a in sos.actors if a.agent is not None
        }
        for actor in sos.actors:
            ag = actor.agent
            if ag is None:
                continue
            where = f"agent: of actor '{actor.id.name}'"
            if ag.invalid is not None:
                self.result.add_error(
                    f"The {where} is '{ag.invalid}'; expected a mapping with "
                    f"kind: (G-2)", actor.loc,
                )
                continue
            if ag.kind not in AGENT_KINDS:
                self.result.add_error(
                    (f"The {where} has kind '{ag.kind}'; expected one of "
                     if ag.kind else f"The {where} has no kind; expected one of ")
                    + f"{', '.join(AGENT_KINDS)} (G-2)", actor.loc,
                )
            if ag.protocol is not None and not is_hyphen_name(ag.protocol):
                self.result.add_error(
                    f"The {where} has protocol '{ag.protocol}', which is not a "
                    f"name such as mcp or a2a (letters, digits, '_' and '-') (G-2)",
                    actor.loc,
                )
            if ag.kind != "tool_server":
                if ag.has_tools and ag.kind in AGENT_KINDS:
                    self.result.add_error(
                        f"The {where} has tools:, which is allowed only for a "
                        f"tool_server, not for a {ag.kind} (G-3)", actor.loc,
                    )
                continue
            # G-3: a tool server lists at least one tool; names are unique
            # identifiers.
            if ag.invalid_tools is not None:
                self.result.add_error(
                    f"tools: in the {where} is '{ag.invalid_tools}'; expected a "
                    f"list of tool names (G-3)", actor.loc,
                )
            elif not ag.tools and not ag.tools_not_names:
                self.result.add_error(
                    f"The {where} is a tool_server and must list at least one "
                    f"tool under tools: (G-3)", actor.loc,
                )
            for text in ag.tools_not_names:
                self.result.add_error(
                    f"tools: in the {where} has the entry '{text}', which is not "
                    f"a name: {self._YAML_NAME_NOTE} (G-3)", actor.loc,
                )
            seen: set[str] = set()
            for tool in ag.tools:
                if not tool:
                    self.result.add_error(
                        f"tools: in the {where} has an empty name (G-3)", actor.loc)
                    continue
                self._check_identifier(tool, f"Tool name in the {where}", actor.loc,
                                       rule="G-3")
                if tool in seen:
                    self.result.add_error(
                        f"Duplicate tool '{tool}' in the {where} (G-3)", actor.loc)
                seen.add(tool)

    def _check_tool_message(self, text, where: str, loc) -> None:
        """G-4: the message a tool server receives is one of its tools."""
        from .agents import parse_message_event
        msg = parse_message_event(text)
        if msg is None or msg.name is None:
            return
        tools = self.tool_servers.get(msg.receiver["name"])
        if tools is not None and msg.name not in tools:
            self.result.add_error(
                f"{where} sends '{msg.name}' to the tool server "
                f"'{msg.receiver['name']}', which does not list it under tools: "
                f"(G-4)", loc,
            )

    def _check_tool_step(self, step: MessageStep, context: str) -> None:
        """G-4 (SHOULD): a message step of a protocol to a tool server."""
        tools = self.tool_servers.get(step.receiver.name)
        message = step.message
        name = getattr(message, "name", None)
        if tools is None or not isinstance(name, str):
            return
        if isinstance(message, ActorRef) and message.index is not None:
            return
        if name not in tools:
            self.result.add_warning(
                f"A message step of {context} sends '{name}' to the tool server "
                f"'{step.receiver.name}', which does not list it under tools: "
                f"(G-4)", step.loc,
            )

    def _check_ungoverned_tool_servers(self, sos: SoSDefinition) -> None:
        """G-15 (b): a tool server that is a party of no enforced contract."""
        governed = {
            p.name for c in sos.contracts if c.enforcement is not None
            for p in c.parties
        }
        for name in self.tool_servers:
            if name not in governed:
                self.result.add_warning(
                    f"The tool server '{name}' is a party of no enforced contract, "
                    f"so its tool calls are ungoverned (G-15)", sos.loc,
                )

    def _check_agent_contract(self, contract: ContractDef, ctx: str) -> None:
        """G-4 to G-18 for one contract."""
        from .agents import (
            ENFORCEMENT_DEFAULTS, ENFORCEMENT_POINTS, HOLD_EVENTS,
            norm_message, parse_message_event, parse_ref, ref_text,
        )
        enf = contract.enforcement
        enforced = enf is not None
        loc = contract.loc
        party_names = {p.name for p in contract.parties}
        events = {r.creates for r in contract.counts_as if r.creates}
        norm_ids = {n.id for n in contract.norms if n.id}

        # G-4: messages to a tool server, wherever a message event is written.
        for norm in contract.norms:
            for key in ("trigger", "until", "content"):
                self._check_tool_message(
                    getattr(norm, key), f"'{key}' of the norm '{norm.id}' of {ctx}",
                    norm.loc)
        for rule in contract.counts_as:
            self._check_tool_message(
                rule.when, f"'when' of the counts_as rule '{rule.creates}' of {ctx}",
                rule.loc)
        if contract.lifecycle:
            for tr in contract.lifecycle.transitions:
                self._check_tool_message(
                    tr.on, f"'on' of the lifecycle transition '{tr.id}' of {ctx}",
                    tr.loc)

        # G-5: enforcement.default and enforcement.point.
        if enforced:
            if enf.invalid is not None:
                self.result.add_error(
                    f"enforcement: of {ctx} is '{enf.invalid}'; expected a mapping "
                    f"with default: and point:, or no value (G-5)", loc,
                )
            if enf.default is not None and enf.default not in ENFORCEMENT_DEFAULTS:
                self.result.add_error(
                    f"enforcement.default of {ctx} is '{enf.default}'; expected "
                    f"deny or allow (G-5)", loc,
                )
            if enf.point is not None and enf.point not in ENFORCEMENT_POINTS:
                self.result.add_error(
                    f"enforcement.point of {ctx} is '{enf.point}'; expected one of "
                    f"{', '.join(ENFORCEMENT_POINTS)} (G-5)", loc,
                )

        # Message contents of the permissions and prohibitions, for the
        # coverage rules (G-15, G-16): (kind, sender, receiver name, name, norm).
        covering = []
        escalate_targets: list[str] = []
        fallbacks = []
        for norm in contract.norms:
            where = f"norm '{norm.id}' of {ctx}"
            msg = norm_message(contract, norm) if norm.content else None
            bearer = ref_text(norm.bearer) if norm.bearer else ""
            if norm.kind in ("permission", "prohibition") and msg is not None \
                    and msg.name is not None:
                covering.append((norm.kind, msg.sender["text"],
                                 msg.receiver["name"], msg.name, norm))
            if enforced and norm.kind in ("permission", "prohibition") and norm.content:
                if msg is None:
                    # G-14 (a)
                    self.result.add_warning(
                        f"The content of the {norm.kind} '{norm.id}' of {ctx} is not "
                        f"a message event, so it has no effect at the boundary and "
                        f"is monitored under Appendix F only (G-14)", norm.loc,
                    )
                else:
                    # G-6
                    if bearer and msg.sender["text"] != bearer:
                        self.result.add_error(
                            f"The content of the {where} is a message sent by "
                            f"'{msg.sender['text']}'; in an enforced contract the "
                            f"sender must be textually equal to its bearer "
                            f"'{norm.bearer}' (G-6)", norm.loc,
                        )
                    # G-14 (b)
                    if msg.has_args:
                        self.result.add_warning(
                            f"The message event content of the {where} has "
                            f"arguments, which are not considered at the boundary "
                            f"(G-14)", norm.loc,
                        )
            # G-9: the names of the hold events are reserved.
            if enforced and norm.id in HOLD_EVENTS:
                self.result.add_error(
                    f"The norm id '{norm.id}' of {ctx} is reserved in an enforced "
                    f"contract for the institutional event of an escalation (G-9)",
                    norm.loc,
                )
            od = norm.on_denied
            if od is None:
                continue

            # G-7: where on_denied: is allowed, and its keys.
            if norm.kind != "prohibition":
                self.result.add_error(
                    f"The {where} is a {norm.kind or 'norm without kind'}; "
                    f"on_denied: is allowed only on a prohibition (G-7)", norm.loc,
                )
            elif msg is None:
                self.result.add_error(
                    f"The {where} has on_denied:, which is allowed only on a "
                    f"prohibition whose content is a message event (G-7)", norm.loc,
                )
            if not enforced:
                self.result.add_error(
                    f"The {where} has on_denied:, but {ctx} is not enforced: it "
                    f"has no enforcement: key (G-7)", norm.loc,
                )
            has_timeout = od.timeout_ms is not None or od.invalid_timeout is not None
            if od.invalid is not None or not (
                    od.escalate_to or has_timeout or od.fallback or od.notify):
                self.result.add_error(
                    f"on_denied: of the {where} must have at least one of "
                    f"escalate_to, timeout, fallback, notify (G-7)", norm.loc,
                )
                continue
            if od.invalid_timeout is not None:
                self.result.add_error(
                    f"on_denied.timeout of the {where} is '{od.invalid_timeout}', "
                    f"which is not a duration: write a number with a unit ms, s, "
                    f"min or h (for example 10min) (G-7)", norm.loc,
                )
            elif od.timeout_ms == 0:
                self.result.add_error(
                    f"on_denied.timeout of the {where} must be greater than zero "
                    f"(G-7)", norm.loc,
                )
            if has_timeout and not od.escalate_to:
                self.result.add_error(
                    f"on_denied.timeout of the {where} requires escalate_to: "
                    f"(G-7)", norm.loc,
                )
            if od.escalate_to and not has_timeout:
                self.result.add_warning(
                    f"on_denied.escalate_to of the {where} has no timeout:, so a "
                    f"held message may be held for ever (G-7)", norm.loc,
                )

            # G-8: escalate_to and notify name single declared actors.
            for key, text in (("escalate_to", od.escalate_to), ("notify", od.notify)):
                if not text:
                    continue
                ref = parse_ref(text)
                if ref["name"] not in self.actor_names:
                    self.result.add_error(
                        f"on_denied.{key} '{text}' of the {where} is not a declared "
                        f"actor (G-8)", norm.loc,
                    )
                    continue
                if ref["kind"] in ("all", "range"):
                    self.result.add_error(
                        f"on_denied.{key} '{text}' of the {where} is a wildcard or "
                        f"a range; name one actor (G-8)", norm.loc,
                    )
                    continue
                if key != "escalate_to":
                    continue
                if norm.kind == "prohibition":
                    escalate_targets.append(ref["text"])
                if ref["name"] not in party_names:
                    self.result.add_error(
                        f"on_denied.escalate_to '{text}' of the {where} is not a "
                        f"party of the contract, so its messages would not be "
                        f"governed (G-8)", norm.loc,
                    )
                if bearer and ref["text"] == bearer:
                    self.result.add_error(
                        f"on_denied.escalate_to of the {where} is its bearer "
                        f"'{norm.bearer}': an actor cannot approve its own message "
                        f"(G-8)", norm.loc,
                    )
                if self.agent_kinds.get(ref["name"]) != "human":
                    self.result.add_warning(
                        f"on_denied.escalate_to '{text}' of the {where} is not an "
                        f"actor with agent.kind: human (G-8)", norm.loc,
                    )

            # G-10: the fallback is another message of the bearer.
            if od.fallback:
                fb = parse_message_event(od.fallback)
                if fb is None:
                    self.result.add_error(
                        f"on_denied.fallback of the {where} must be a message event "
                        f"(A -> B : msg), not '{od.fallback}' (G-10)", norm.loc,
                    )
                else:
                    if bearer and fb.sender["text"] != bearer:
                        self.result.add_error(
                            f"on_denied.fallback of the {where} is sent by "
                            f"'{fb.sender['text']}'; the sender must be textually "
                            f"equal to the bearer '{norm.bearer}' (G-10)", norm.loc,
                        )
                    if fb.receiver["name"] not in self.actor_names:
                        self.result.add_error(
                            f"The receiver '{fb.receiver['text']}' of "
                            f"on_denied.fallback of the {where} is not a declared "
                            f"actor (G-10)", norm.loc,
                        )
                    if msg is not None and fb.receiver["text"] == msg.receiver["text"] \
                            and fb.name == msg.name:
                        self.result.add_error(
                            f"on_denied.fallback of the {where} must differ from "
                            f"the content of the prohibition in receiver or message "
                            f"name (G-10)", norm.loc,
                        )
                    self._check_tool_message(
                        od.fallback, f"on_denied.fallback of the {where}", norm.loc)
                    if fb.name is not None:
                        fallbacks.append((f"on_denied.fallback of the {where}", fb))

        # G-9: approval and rejection are created by the approver's messages.
        hold_rules = []
        for rule in contract.counts_as:
            if rule.creates in HOLD_EVENTS:
                hold_rules.append((rule, parse_message_event(rule.when)))
        for target in dict.fromkeys(escalate_targets):
            created = {r.creates for r, m in hold_rules
                       if m is not None and m.sender["text"] == target}
            if "approval" not in created:
                self.result.add_error(
                    f"{ctx} has escalate_to: {target} but no counts_as rule that "
                    f"creates 'approval' from a message event sent by {target} "
                    f"(G-9)", loc,
                )
            if "rejection" not in created:
                self.result.add_warning(
                    f"{ctx} has escalate_to: {target} but no counts_as rule that "
                    f"creates 'rejection' from a message event sent by {target} "
                    f"(G-9)", loc,
                )
        if enforced:
            for rule, m in hold_rules:
                if m is None or m.sender["text"] not in escalate_targets:
                    self.result.add_error(
                        f"The counts_as rule that creates '{rule.creates}' in {ctx} "
                        f"must have as when: a message event whose sender is the "
                        f"escalate_to: of a prohibition of the contract; the name "
                        f"is reserved in an enforced contract (G-9)", rule.loc,
                    )

        self._check_delegations(contract, ctx, events, norm_ids)

        if not enforced:
            return
        self._check_event_parties(contract, ctx, events)
        permissions = [c for c in covering if c[0] == "permission"]
        prohibitions = [c for c in covering if c[0] == "prohibition"]

        def is_tool(receiver: str, name: str) -> bool:
            return name in self.tool_servers.get(receiver, ())

        # G-15 (a): tools left open by `default: allow`.
        if enf.default == "allow":
            for party in dict.fromkeys(p.name for p in contract.parties):
                open_tools = [
                    t for t in self.tool_servers.get(party, [])
                    if not any(c[2] == party and c[3] == t for c in covering)
                ]
                if open_tools:
                    self.result.add_warning(
                        f"{ctx} has default: allow, and no permission or prohibition "
                        f"covers the tool{'s' if len(open_tools) > 1 else ''} "
                        f"{', '.join(open_tools)} of its party '{party}' (G-15)", loc,
                    )

        # G-16 (a): a message the contract needs and would itself deny.
        needed = list(fallbacks)
        for norm in contract.norms:
            if norm.kind == "obligation":
                m = norm_message(contract, norm) if norm.content else None
                if m is not None and m.name is not None:
                    needed.append((f"content of the obligation '{norm.id}' of {ctx}", m))
        for what, m in needed:
            recv, name = m.receiver["name"], m.name
            if (is_tool(recv, name) and enf.default != "allow"
                    and not any(c[1] == m.sender["text"] and c[2] == recv and c[3] == name
                                for c in permissions)):
                self.result.add_warning(
                    f"The {what} is the tool call '{m.text}', which no permission "
                    f"of its sender covers; the contract would deny it by default "
                    f"(G-16)", loc,
                )
            for c in prohibitions:
                # Only a prohibition of the sender itself would deny it.
                if (c[2] == recv and c[3] == name and not c[4].trigger
                        and c[4].bearer and ref_text(c[4].bearer) == m.sender["text"]):
                    self.result.add_warning(
                        f"The {what} is '{m.text}', which the prohibition "
                        f"'{c[4].id}' of its sender, without trigger:, covers; the "
                        f"contract would deny it (G-16)", loc,
                    )

        # G-16 (b): under default: deny, the messages of delegation and
        # escalation need a permission.
        if enf.default == "deny":
            for d in contract.delegations:
                for key, name in (("grant", d.grant), ("revoke", d.revoke)):
                    if name and not any(c[3] == name for c in permissions):
                        self.result.add_warning(
                            f"{ctx} has default: deny, and no permission covers the "
                            f"{key}: message '{name}' of the delegation of "
                            f"'{d.norm}', so it would be denied (G-16)", loc,
                        )
            for rule, m in hold_rules:
                if m is not None and m.name is not None and not any(
                        c[2] == m.receiver["name"] and c[3] == m.name
                        for c in permissions):
                    self.result.add_warning(
                        f"{ctx} has default: deny, and no permission covers "
                        f"'{m.text}', the when: of the rule that creates "
                        f"'{rule.creates}', so it would be denied (G-16)", loc,
                    )

    def _check_event_parties(self, contract: ContractDef, ctx: str, events: set[str]) -> None:
        """G-17 and G-18: an instance of an enforced contract observes a
        message only when the contract governs it (G.5), so a message event
        of the contract that a non-party could send or receive would never
        occur; and an index that no party binds has no value the file
        accounts for."""
        from .agents import (
            _actor_entry,
            _party_ref,
            parse_message_event,
            parse_ref,
            ref_covered,
        )
        try:
            parties = [_party_ref(p) for p in contract.parties]
        except Exception:
            return
        bound = {p["var"] for p in parties if p["kind"] == "var"}
        actors = {a.id.name: _actor_entry(a) for a in self.sos_actors}

        def unbound(ref: dict, what: str, loc) -> None:
            """G-18: every index variable is the index of a party."""
            if ref["kind"] == "var" and ref["var"] not in bound:
                self.result.add_error(
                    f"The index '{ref['var']}' of {what} in {ctx} is not bound by a "
                    f"party of the contract (no party is written with [{ref['var']}]); "
                    f"a contract instance binds only the indices of its parties (G-18)",
                    loc,
                )

        def outside(ref: dict):
            """A literal index or a range that leaves the declared set."""
            decl = actors[ref["name"]]
            if not decl["indexed"] or ref["kind"] not in ("index", "range"):
                return False
            lo = ref["index"] if ref["kind"] == "index" else ref["lo"]
            hi = ref["index"] if ref["kind"] == "index" else ref["hi"]
            return (decl["lo"] is not None and lo < decl["lo"]) or (
                decl["bounded"] and hi > decl["hi"])

        items = []      # where, text, location
        for n in contract.norms:
            for key in ("trigger", "until", "content"):
                text = getattr(n, key)
                if text and text.strip() not in events:
                    items.append((f"{key}: of norm '{n.id}'", text, n.loc))
            od = n.on_denied
            texts = [("bearer:", n.bearer), ("counterparty:", n.counterparty),
                     ("on_denied.escalate_to:", od.escalate_to if od else None),
                     ("on_denied.notify:", od.notify if od else None)]
            for key, text in texts:
                if not text:
                    continue
                try:
                    ref = parse_ref(text)
                except Exception:
                    continue
                unbound(ref, f"'{ref['text']}', the {key} of norm '{n.id}'", n.loc)
            fb = None
            if od is not None and od.fallback:
                try:
                    fb = parse_message_event(od.fallback)
                except Exception:
                    fb = None
            if fb is not None:
                for ref in (fb.sender, fb.receiver):
                    unbound(ref, f"'{ref['text']}' in on_denied.fallback: of norm "
                                 f"'{n.id}'", n.loc)
        for r in contract.counts_as:
            items.append((f"when: of the counts_as rule that creates '{r.creates}'",
                          r.when, r.loc))
        for d in contract.delegations:
            if d.until and d.until.strip() not in events:
                items.append((f"until: of the delegation of '{d.norm}'", d.until, d.loc))
        for where, text, loc in items:
            try:
                m = parse_message_event(text)
            except Exception:
                m = None
            if m is None:
                continue
            to_party_tool = (ref_covered(m.receiver, parties, actors)
                             and self.agent_kinds.get(m.receiver["name"]) == "tool_server")
            for role, ref in (("sender", m.sender), ("receiver", m.receiver)):
                if ref["name"] not in actors:
                    continue                    # reported by N-9
                if outside(ref):
                    self.result.add_error(
                        f"The {role} '{ref['text']}' of the {where} in {ctx} names "
                        f"actors outside the declared set '{ref['name']}'; for them "
                        f"this message event could never occur (G-17)", loc,
                    )
                elif not ref_covered(ref, parties, actors):
                    why = ("a tool call of an actor that is not a party is governed and "
                           "is decided by the default and by the norms written for "
                           "parties; a contract cannot make it the event of a norm"
                           if role == "sender" and to_party_tool else
                           "an enforced contract observes only the messages it governs, "
                           "so this message event could never occur")
                    self.result.add_error(
                        f"The {role} '{ref['text']}' of the {where} in {ctx} is not "
                        f"covered by one reference in the parties of the contract; "
                        f"{why} (G-17)", loc,
                    )
                else:
                    unbound(ref, f"the {role} '{ref['text']}' of the {where}", loc)

    def _check_delegations(
        self, contract: ContractDef, ctx: str, events: set[str], norm_ids: set[str],
    ) -> None:
        """G-11 to G-13: the `delegations:` of a contract."""
        from .agents import HOLD_EVENTS, norm_message, parse_ref
        loc = contract.loc
        if contract.invalid_delegations is not None:
            self.result.add_error(
                f"delegations: of {ctx} is '{contract.invalid_delegations}'; "
                f"expected a list of entries with norm:, to: and grant: (G-11)", loc,
            )
        if not contract.delegations:
            return
        if contract.enforcement is None:
            self.result.add_error(
                f"{ctx} has delegations:, which is allowed only in an enforced "
                f"contract: it has no enforcement: key (G-11)", loc,
            )
        norms_by_id: dict[str, NormDef] = {}
        for n in contract.norms:
            norms_by_id.setdefault(n.id, n)
        party_tools = {
            tool: party for party in sorted({p.name for p in contract.parties})
            for tool in self.tool_servers.get(party, [])
        }
        seen_norms: set[str] = set()
        names: list[str] = []
        for d in contract.delegations:
            where = (f"delegation of '{d.norm}' in {ctx}" if d.norm
                     else f"delegation without norm: in {ctx}")
            # G-11: the delegable permission.
            bearer = None
            if not d.norm or "norm" in d.not_names:
                self.result.add_error(
                    f"A delegation in {ctx} must name with norm: the id of a "
                    f"permission of the contract (G-11)", d.loc,
                )
            else:
                target = norms_by_id.get(d.norm)
                if target is None:
                    self.result.add_error(
                        f"The {where} names '{d.norm}', which is not a norm of "
                        f"{ctx} (G-11)", d.loc,
                    )
                elif target.kind != "permission" or norm_message(contract, target) is None:
                    self.result.add_error(
                        f"The {where} names '{d.norm}', which is not a permission "
                        f"whose content is a message event; only such a permission "
                        f"can be delegated (G-11)", d.loc,
                    )
                elif target.bearer:
                    bearer = parse_ref(target.bearer)
                    if bearer["kind"] == "var":
                        self.result.add_error(
                            f"The {where} names a permission whose bearer "
                            f"'{target.bearer}' has an index bound by the contract "
                            f"instance; the first actor of a chain must be readable "
                            f"from the file (G-11)", d.loc,
                        )
                        bearer = None
                if d.norm in seen_norms:
                    self.result.add_error(
                        f"More than one delegation in {ctx} names the norm "
                        f"'{d.norm}'; at most one entry may name a norm (G-11)", d.loc,
                    )
                seen_norms.add(d.norm)

            # G-12: to, max_depth, until.
            if not d.to:
                self.result.add_error(
                    f"The {where} must list at least one actor under to: (G-12)",
                    d.loc,
                )
            for text in d.to:
                ref = parse_ref(text)
                if ref["name"] not in self.actor_names:
                    self.result.add_error(
                        f"'{text}' in to: of the {where} is not a declared actor "
                        f"(G-12)", d.loc,
                    )
                elif ref["kind"] == "var":
                    self.result.add_error(
                        f"'{text}' in to: of the {where} has an index bound by the "
                        f"contract instance; use an actor, a wildcard or a range "
                        f"(G-12)", d.loc,
                    )
                elif ref["name"] in self.tool_servers:
                    self.result.add_error(
                        f"'{text}' in to: of the {where} is a tool server, which "
                        f"cannot hold a delegated permission (G-12)", d.loc,
                    )
                elif bearer is not None and ref["name"] == bearer["name"] and (
                        ref["text"] == bearer["text"] or ref["kind"] == "all"
                        or (ref["kind"] == "range" and bearer["kind"] == "index"
                            and ref["lo"] <= bearer["index"] <= ref["hi"])):
                    self.result.add_warning(
                        f"to: of the {where} includes '{text}', the bearer of the "
                        f"permission, which holds it already (G-12)", d.loc,
                    )
            if d.invalid_max_depth is not None:
                self.result.add_error(
                    f"max_depth of the {where} is '{d.invalid_max_depth}'; expected "
                    f"an integer of at least 1 (G-12)", d.loc,
                )
            if d.until:
                self._check_norm_term(
                    d.until, f"'until' of the {where}", d.loc, events, norm_ids)

            # G-13: the grant and revoke messages.
            for key, name in (("grant", d.grant), ("revoke", d.revoke)):
                if key in d.not_names:
                    self.result.add_error(
                        f"{key}: of the {where} is '{name}', which is not a name: "
                        f"{self._YAML_NAME_NOTE} (G-13)", d.loc,
                    )
                    continue
                if not name:
                    if key == "grant":
                        self.result.add_error(
                            f"The {where} has no grant:, the name of the message "
                            f"that delegates (G-13)", d.loc,
                        )
                    continue
                self._check_identifier(name, f"{key}: of the {where}", d.loc,
                                       rule="G-13")
                if name in names:
                    self.result.add_error(
                        f"{key}: '{name}' of the {where} is used more than once; "
                        f"all grant: and revoke: names of a contract must differ "
                        f"(G-13)", d.loc,
                    )
                if name in HOLD_EVENTS:
                    self.result.add_error(
                        f"{key}: '{name}' of the {where} must differ from approval "
                        f"and rejection (G-13)", d.loc,
                    )
                if name in party_tools:
                    self.result.add_error(
                        f"{key}: '{name}' of the {where} is a tool of the tool "
                        f"server '{party_tools[name]}', a party of the contract "
                        f"(G-13)", d.loc,
                    )
                names.append(name)

    def _check_actor_ref(self, ref: ActorRef, context: str) -> None:
        """Check that an actor reference refers to a defined actor."""
        if ref.name not in self.actor_names:
            self.result.add_error(
                f"Undefined actor '{ref.name}' referenced in {context}",
                ref.loc,
            )

    def _check_actor_ref_in_expr(
        self, expr: Expression, context: str, bound: frozenset = frozenset()
    ) -> None:
        """Recursively check actor references in expressions.

        The expression grammar reads every bare name as an actor
        reference, so a name standing alone (``system_ready``,
        ``delivery_time``) cannot be told from a state variable and is
        not reported. A name is treated as an actor, and must be
        declared, when it is indexed (``ROBOT[i]``) or is the object of
        a member access (``ROBOT.battery``). Variables bound by a
        quantifier or a comprehension are exempt.
        """
        if isinstance(expr, ActorRef):
            if expr.index is not None and expr.name not in bound:
                self._check_actor_ref(expr, context)
            self._check_index_expr(expr, context, bound)
        elif isinstance(expr, MemberAccess):
            if expr.obj.name not in bound:
                self._check_actor_ref(expr.obj, context)
            self._check_index_expr(expr.obj, context, bound)
        elif isinstance(expr, FunctionCall):
            for arg in expr.args:
                self._check_actor_ref_in_expr(arg, context, bound)
        elif isinstance(expr, BinaryOp):
            self._check_actor_ref_in_expr(expr.left, context, bound)
            self._check_actor_ref_in_expr(expr.right, context, bound)
        elif isinstance(expr, UnaryOp):
            self._check_actor_ref_in_expr(expr.operand, context, bound)
        elif isinstance(expr, QuantifiedExpr):
            self._check_actor_ref_in_expr(expr.domain, context, bound)
            self._check_actor_ref_in_expr(
                expr.predicate, context, bound | {expr.variable}
            )
        elif isinstance(expr, Comprehension):
            if not isinstance(expr.domain, RangeExpr):
                self._check_actor_ref_in_expr(expr.domain, context, bound)
            self._check_actor_ref_in_expr(
                expr.element, context, bound | {expr.variable}
            )

    def _check_index_expr(self, ref: ActorRef, context: str, bound: frozenset) -> None:
        """Check actor references inside an index, e.g. ``ROBOT[GHOST.n]``."""
        index = ref.index
        if index is None or isinstance(index, (str, int, RangeExpr)):
            return
        self._check_actor_ref_in_expr(index, context, bound)

    _SEVERITIES = ("Minor", "Major", "Critical")

    def _check_severities(self, contract: ContractDef, ctx: str) -> None:
        """A severity outside the three levels has no meaning to the
        generated runtimes (the C# target would emit an unknown enum member)."""
        def check(value, where: str) -> None:
            if value is not None and value not in self._SEVERITIES:
                self.result.add_error(
                    f"Unknown severity '{value}' in {ctx} {where}; "
                    f"expected one of {', '.join(self._SEVERITIES)}",
                    contract.loc,
                )
        if contract.lifecycle:
            for tr in contract.lifecycle.transitions:
                if tr.on_violation:
                    check(tr.on_violation.severity, f"lifecycle transition '{tr.id}'")
        for mon in contract.monitors:
            if mon.on_match:
                check(mon.on_match.severity, f"monitor '{mon.id}'")
        if self.norms_declared:
            for norm in contract.norms:
                if norm.on_violation:
                    check(norm.on_violation.severity, f"norm '{norm.id}'")

    def _check_contracts(self, sos: SoSDefinition) -> None:
        """Check contract definitions."""
        for contract in sos.contracts:
            self._check_contract(contract)

    def _check_contract(self, contract: ContractDef) -> None:
        ctx = f"contract '{contract.id}'"

        # Check parties exist and are unique
        # Two references are the same party only when name and index agree:
        # ROBOT[1] and ROBOT[2] are different parties.
        from .unparse import actor_ref_to_source
        party_refs: list[str] = []
        for party in contract.parties:
            self._check_actor_ref(party, f"{ctx} parties")
            ref = actor_ref_to_source(party)
            if ref in party_refs:
                self.result.add_error(
                    f"Duplicate party '{ref}' in {ctx}",
                    contract.loc,
                )
            party_refs.append(ref)

        if not contract.parties:
            self.result.add_error(
                f"Contract '{contract.id}' has no parties defined",
                contract.loc,
            )

        # Check assume/guarantee expressions
        for pred in contract.assume:
            self._check_actor_ref_in_expr(pred, f"{ctx} assume")
        for pred in contract.guarantee:
            self._check_actor_ref_in_expr(pred, f"{ctx} guarantee")

        # Check authority block
        if contract.authority:
            self._check_authority(contract.authority, ctx)

        # Check information block
        if contract.information:
            self._check_information(contract.information, ctx)

        # Check responsibilities
        for resp in contract.responsibilities:
            self._check_actor_ref(resp.actor, f"{ctx} responsibilities")

        # Check incentives
        if contract.incentives:
            self._check_incentives(contract.incentives, ctx)

        self._check_severities(contract, ctx)
        self._check_lifecycle(contract, ctx)
        self._check_monitors(contract, ctx)

        # Norms extension (Appendix F): checked only when declared (F.11).
        if self.norms_declared:
            self._check_temporal_placement(contract, ctx)
            self._check_norms(contract, ctx)
        else:
            keys = [k for k, v in (("norms:", contract.norms),
                                   ("counts_as:", contract.counts_as)) if v]
            if keys:
                self.result.add_warning(
                    f"{ctx} uses {' and '.join(keys)}, which belong to the norms "
                    f"extension, but the file does not declare 'norms: 0.1' under "
                    f"extensions:; they are undeclared extension keys, and are "
                    f"neither checked nor lowered to the IR (Appendix F, F.11)",
                    contract.loc,
                )

        # Agent governance extension (Appendix G): checked only when declared.
        if self.agents_active:
            self._check_agent_contract(contract, ctx)

    def _check_authority(self, auth: AuthorityBlock, context: str) -> None:
        """Check authority block constraints."""
        if auth.decision_holder:
            self._check_actor_ref(auth.decision_holder, f"{context} authority")

        if auth.beta is not None:
            if not (0.0 <= auth.beta <= 1.0):
                self.result.add_error(
                    f"Authority beta parameter must be in [0, 1], got {auth.beta} in {context}",
                )

    def _check_information(self, info: InformationBlock, context: str) -> None:
        """Check information block constraints."""
        if info.alpha is not None:
            if not (0.0 <= info.alpha <= 1.0):
                self.result.add_error(
                    f"Information alpha parameter must be in [0, 1], got {info.alpha} in {context}",
                )

        for view in info.views:
            self._check_actor_ref(view.actor, f"{context} information views")

        for sharing in info.sharing:
            self._check_actor_ref(sharing.source, f"{context} information sharing")
            self._check_actor_ref(sharing.target, f"{context} information sharing")

        for entry in info.lenient_sharing:
            self.result.add_warning(
                f"Sharing entry in {context} has an item that is not an identifier: "
                f"{entry!r}. Only the item name is kept; the rest is ignored",
            )

        for entry in info.invalid_sharing:
            self.result.add_error(
                f"Invalid sharing entry in {context}: {entry!r}. A sharing entry "
                f"must be a quoted string of the form \"SOURCE -> TARGET : item\", "
                f"where item is an identifier",
            )

    def _check_incentives(self, inc: IncentivesBlock, context: str) -> None:
        """Check incentives block constraints."""
        if inc.lambda_ is not None:
            if not (0.0 <= inc.lambda_ <= 1.0):
                self.result.add_error(
                    f"Incentive lambda parameter must be in [0, 1], got {inc.lambda_} in {context}",
                )

    def _check_protocols(self, sos: SoSDefinition) -> None:
        """Check protocol definitions."""
        for protocol in sos.protocols:
            self._check_protocol(protocol)

    def _check_protocol(self, protocol: ProtocolDef) -> None:
        ctx = f"protocol '{protocol.id}'"
        for step in protocol.steps:
            self._check_step(step, ctx)

    def _check_step(self, step: Step, context: str) -> None:
        """Check a protocol step recursively."""
        if isinstance(step, MessageStep):
            self._check_actor_ref(step.sender, f"{context} message step sender")
            self._check_actor_ref(step.receiver, f"{context} message step receiver")
            if self.agents_active:
                self._check_tool_step(step, context)
        elif isinstance(step, ComputeStep):
            self._check_actor_ref(step.actor, f"{context} compute step")
        elif isinstance(step, ConditionalStep):
            for s in step.then_steps:
                self._check_step(s, context)
            for s in step.else_steps:
                self._check_step(s, context)
        elif isinstance(step, ParallelStep):
            for s in step.steps:
                self._check_step(s, context)
        elif isinstance(step, BarrierStep):
            pass  # barrier condition is an expression

    def _check_transitions(self, sos: SoSDefinition) -> None:
        """Check transition definitions."""
        regime_names: set[str] = set()
        for trans in sos.transitions:
            for regime in (trans.from_regime, trans.to_regime):
                if regime not in regime_names:
                    self._check_identifier(regime, "Regime name", trans.loc)
                regime_names.add(regime)

            if trans.protocol and trans.protocol not in self.protocol_ids:
                self.result.add_warning(
                    f"Transition references undefined protocol '{trans.protocol}'",
                    trans.loc,
                )

            # Validate condition expression is parseable
            if trans.condition:
                try:
                    from .parser import parse_expr
                    parse_expr(trans.condition)
                except Exception:
                    self.result.add_warning(
                        f"Transition {trans.from_regime}->{trans.to_regime} condition "
                        f"is not a valid expression: {trans.condition}",
                        trans.loc,
                    )

            # Validate safety_invariant expression is parseable
            if trans.safety_invariant:
                try:
                    from .parser import parse_expr
                    parse_expr(trans.safety_invariant)
                except Exception:
                    self.result.add_warning(
                        f"Transition {trans.from_regime}->{trans.to_regime} safety_invariant "
                        f"is not a valid expression: {trans.safety_invariant}",
                        trans.loc,
                    )

        # Check for orphan regimes (from_regime that is never a to_regime and vice versa)
        from_regimes = {t.from_regime for t in sos.transitions}
        to_regimes = {t.to_regime for t in sos.transitions}
        only_targets = to_regimes - from_regimes
        if only_targets and len(regime_names) > 1:
            for name in only_targets:
                self.result.add_warning(
                    f"Regime '{name}' is a dead-end (no outgoing transitions)",
                )

    def _check_algorithms(self, sos: SoSDefinition) -> None:
        """Check algorithm definitions for duplicates."""
        seen: set[str] = set()
        for algo in sos.algorithms:
            if algo.name in seen:
                self.result.add_warning(
                    f"Duplicate algorithm name: '{algo.name}'",
                )
            seen.add(algo.name)

    def _check_metrics(self, sos: SoSDefinition) -> None:
        """Check metric definitions."""
        seen: set[str] = set()
        for metric in sos.metrics:
            if metric.id not in seen:
                self._check_identifier(metric.id, "Metric ID", metric.loc)
            if metric.id in seen:
                self.result.add_error(
                    f"Duplicate metric ID: '{metric.id}'",
                    metric.loc,
                )
            seen.add(metric.id)
            # Validate formula is parseable
            if metric.formula:
                try:
                    from .parser import parse_expr
                    parse_expr(metric.formula)
                except Exception:
                    self.result.add_warning(
                        f"Metric '{metric.id}' formula is not a valid expression: {metric.formula}",
                        metric.loc,
                    )


    def _check_identifier(
        self, name: str, what: str, loc: SourceLocation | None = None, rule: str = ""
    ) -> None:
        """Identifiers are ASCII and are not reserved words (Appendix A §A.1, §A.11)."""
        if not name:
            return  # a missing id is reported elsewhere
        tag = f" ({rule})" if rule else ""
        if name in RESERVED_WORDS:
            self.result.add_error(
                f"{what} '{name}' is a reserved word and cannot be used as an "
                f"identifier{tag}",
                loc,
            )
        elif not _IDENTIFIER_RE.fullmatch(name):
            self.result.add_error(
                f"{what} '{name}' is not a valid identifier: use ASCII letters, "
                f"digits and '_' only, not starting with a digit{tag}",
                loc,
            )

    def _check_codegen(self, sos: SoSDefinition) -> None:
        """A target that cannot be emitted gets a diagnostic (Appendix D §D.4)."""
        for spec in sos.codegen:
            target = spec.target
            if target in CODEGEN_TARGETS:
                continue
            if target in SIM_TARGETS:
                self.result.add_warning(
                    f"codegen target '{target}' is a simulator config; it is not "
                    f"emitted by `cadl codegen`, use `cadl sim-gen -t {target}`",
                    spec.loc,
                )
            else:
                self.result.add_warning(
                    f"codegen target '{target}' is not supported by this processor "
                    f"(supported: {', '.join(CODEGEN_TARGETS)})",
                    spec.loc,
                )

    def _check_extensions(self, sos: SoSDefinition) -> None:
        """Informational diagnostics for extensions this processor does not implement."""
        for name, version in sos.extensions:
            if name not in KNOWN_EXTENSIONS:
                self.result.add_info(
                    f"extension '{name}' is not known to this processor; "
                    f"its declaration is ignored",
                    sos.loc,
                )
            elif version and version not in KNOWN_EXTENSIONS[name]:
                self.result.add_info(
                    f"extension '{name}' is declared with version '{version}'; this "
                    f"processor implements {', '.join(KNOWN_EXTENSIONS[name])}",
                    sos.loc,
                )
        if sos.motivation is not None:
            self.result.add_info(
                "the motivation: block (Appendix C) is not interpreted by this "
                "processor; it is kept as written and passed on by `cadl sim-ir`",
                sos.loc,
            )


def type_check(sos: SoSDefinition) -> TypeCheckResult:
    """Run type checking on a CADL AST and return the result."""
    checker = TypeChecker()
    return checker.check(sos)
