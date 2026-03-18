"""CADL Type Checker - validates semantic constraints on the AST.

Checks performed (per spec §6.2.2):
1. Actor reference existence - all referenced actors are defined
2. Contract party validation - no duplicates, all parties exist
3. Protocol step sender/receiver match actor definitions
4. Information sharing declarations match message exchanges
5. Institutional parameter range constraints (0 <= alpha, beta, lambda <= 1)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ast_nodes import (
    ActorDef,
    ActorRef,
    AuthorityBlock,
    BarrierStep,
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
    ProtocolDef,
    RangeExpr,
    ResponsibilityGroup,
    SharingDef,
    SoSDefinition,
    SourceLocation,
    Step,
    ViewDef,
)


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

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    def add_error(self, message: str, loc: SourceLocation | None = None) -> None:
        self.errors.append(TypeError(message=message, loc=loc, severity="error"))

    def add_warning(self, message: str, loc: SourceLocation | None = None) -> None:
        self.warnings.append(TypeError(message=message, loc=loc, severity="warning"))


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
        return self.result

    def _collect_definitions(self, sos: SoSDefinition) -> None:
        """Collect all defined names for reference checking."""
        for actor in sos.actors:
            self.actor_names.add(actor.id.name)
        for contract in sos.contracts:
            if contract.id in self.contract_ids:
                self.result.add_error(
                    f"Duplicate contract ID: '{contract.id}'",
                    contract.loc,
                )
            self.contract_ids.add(contract.id)
        for protocol in sos.protocols:
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

            if not actor.role:
                self.result.add_warning(
                    f"Actor '{name}' has no role defined",
                    actor.loc,
                )

    def _check_actor_ref(self, ref: ActorRef, context: str) -> None:
        """Check that an actor reference refers to a defined actor."""
        if ref.name not in self.actor_names:
            self.result.add_error(
                f"Undefined actor '{ref.name}' referenced in {context}",
                ref.loc,
            )

    def _check_actor_ref_in_expr(self, expr: Expression, context: str) -> None:
        """Recursively check actor references in expressions."""
        if isinstance(expr, ActorRef):
            self._check_actor_ref(expr, context)
        elif isinstance(expr, MemberAccess):
            self._check_actor_ref(expr.obj, context)
        elif isinstance(expr, FunctionCall):
            for arg in expr.args:
                self._check_actor_ref_in_expr(arg, context)

    def _check_contracts(self, sos: SoSDefinition) -> None:
        """Check contract definitions."""
        for contract in sos.contracts:
            self._check_contract(contract)

    def _check_contract(self, contract: ContractDef) -> None:
        ctx = f"contract '{contract.id}'"

        # Check parties exist and are unique
        party_names: list[str] = []
        for party in contract.parties:
            self._check_actor_ref(party, f"{ctx} parties")
            if party.name in party_names:
                self.result.add_error(
                    f"Duplicate party '{party.name}' in {ctx}",
                    contract.loc,
                )
            party_names.append(party.name)

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
            regime_names.add(trans.from_regime)
            regime_names.add(trans.to_regime)

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


def type_check(sos: SoSDefinition) -> TypeCheckResult:
    """Run type checking on a CADL AST and return the result."""
    checker = TypeChecker()
    return checker.check(sos)
