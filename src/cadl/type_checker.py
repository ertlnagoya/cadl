"""CADL Type Checker - validates semantic constraints on the AST.

Checks performed (per spec §6.2.2):
1. Actor reference existence - all referenced actors are defined
2. Contract party validation - no duplicates, all parties exist
3. Protocol step sender/receiver match actor definitions
4. Information sharing declarations name declared actors (comparing
   them with the actual message exchanges is planned, not implemented)
5. Institutional parameter range constraints (0 <= alpha, beta, lambda <= 1)
6. SoS-DSL static semantics, rules L-1 to M-3 (Appendix E §E.4)
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
    Expression,
    FunctionCall,
    IncentivesBlock,
    InformationBlock,
    MemberAccess,
    MessageStep,
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
CODEGEN_TARGETS = ("python", "solidity", "opa", "unity-csharp")
SIM_TARGETS = ("unity", "go")
# Extensions this processor implements, with the versions it knows
KNOWN_EXTENSIONS = {"sos-dsl": ("0.1",)}


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
        self.contract_ids: set[str] = set()
        self.protocol_ids: set[str] = set()

    def check(self, sos: SoSDefinition) -> TypeCheckResult:
        """Run all type checks on the SoS definition."""
        self.result = TypeCheckResult()
        self._collect_definitions(sos)
        self._check_actors(sos)
        self._check_contracts(sos)
        self._check_protocols(sos)
        self._check_transitions(sos)
        self._check_algorithms(sos)
        self._check_metrics(sos)
        self._check_codegen(sos)
        self._check_extensions(sos)
        return self.result

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


    def _check_identifier(self, name: str, what: str, loc: SourceLocation | None = None) -> None:
        """Identifiers are ASCII and are not reserved words (Appendix A §A.1, §A.11)."""
        if not name:
            return  # a missing id is reported elsewhere
        if name in RESERVED_WORDS:
            self.result.add_error(
                f"{what} '{name}' is a reserved word and cannot be used as an identifier",
                loc,
            )
        elif not _IDENTIFIER_RE.fullmatch(name):
            self.result.add_error(
                f"{what} '{name}' is not a valid identifier: use ASCII letters, "
                f"digits and '_' only, not starting with a digit",
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
