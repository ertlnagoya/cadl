"""Tests for VerificationSpec method dispatch (Appendix A §A.8).

Ensures the reference verifier emits explicit `not_supported` /
`failed` results for non-SMT methods instead of silently skipping.
Also exercises the optional motivation extension (Appendix C).
"""
from __future__ import annotations

import pytest

from cadl.ast_nodes import (
    AgentMotivationBlock,
    GovernanceMotivationBlock,
    MotivationBlock,
    SoSDefinition,
    VerificationSpec,
)
from cadl.verifier import (
    _KNOWN_METHODS,
    _SUPPORTED_METHODS,
    dispatch_spec,
    verify,
)


class TestMethodDispatch:
    def test_smt_method_passes(self):
        r = dispatch_spec(VerificationSpec(id="v1", type="safety", method="smt"))
        assert r.status == "passed"
        assert "smt" in r.message.lower()

    def test_default_method_is_smt(self):
        r = dispatch_spec(VerificationSpec(id="v1", type="safety"))
        assert r.status == "passed"

    @pytest.mark.parametrize("method", ["model_check", "simulation", "proof"])
    def test_known_unsupported_methods(self, method: str):
        r = dispatch_spec(VerificationSpec(id="v1", type="safety", method=method))
        assert r.status == "not_supported"
        assert method in r.message

    def test_unknown_method_fails(self):
        r = dispatch_spec(
            VerificationSpec(id="v1", type="safety", method="telepathy")
        )
        assert r.status == "failed"
        assert "telepathy" in r.message

    def test_method_set_matches_spec(self):
        # Appendix A §A.8 enumeration
        assert _KNOWN_METHODS == {"smt", "model_check", "simulation", "proof"}
        assert _SUPPORTED_METHODS == {"smt"}


class TestVerifyIntegratesDispatch:
    def test_verify_appends_spec_dispatch_results(self):
        sos = SoSDefinition(
            name="t",
            verifications=[
                VerificationSpec(id="v_sim", type="safety", method="simulation"),
                VerificationSpec(id="v_smt", type="safety", method="smt"),
            ],
        )
        results = verify(sos)
        check_names = {r.check_name for r in results}
        assert "verification.v_sim" in check_names
        assert "verification.v_smt" in check_names


_SOURCE_WITH_METHODS = """
sos:
  name: "MethodFromSource"
  type: Collaborative
  actors:
    - id: A
      role: "worker"
  verification:
    - id: v_default
      type: consistency
    - id: v_smt
      type: consistency
      method: SMT
    - id: v_mc
      type: safety
      method: model_check
      expr: "x > 0"
      bound: 100
    - id: v_bad_bound
      type: safety
      method: simulation
      bound: "1000_steps"
"""


class TestMethodReadFromSource:
    """`method` / `expr` / `bound` written in a .cadl file reach the verifier."""

    def test_parser_reads_method_expr_bound(self):
        from cadl.parser import parse

        specs = {v.id: v for v in parse(_SOURCE_WITH_METHODS).verifications}
        assert specs["v_default"].method is None
        assert specs["v_smt"].method == "SMT"
        assert specs["v_mc"].method == "model_check"
        assert specs["v_mc"].expr == "x > 0"
        assert specs["v_mc"].bound == 100
        assert specs["v_bad_bound"].bound is None

    def test_non_smt_method_in_source_is_reported_not_supported(self):
        from cadl.parser import parse

        results = {r.check_name: r for r in verify(parse(_SOURCE_WITH_METHODS))}
        assert results["verification.v_default"].status == "passed"
        assert results["verification.v_smt"].status == "passed"
        assert results["verification.v_mc"].status == "not_supported"
        assert results["verification.v_bad_bound"].status == "not_supported"


class TestMotivationExtension:
    def test_sos_definition_has_optional_motivation_field(self):
        sos = SoSDefinition(name="t")
        assert sos.motivation is None

    def test_motivation_block_can_be_attached(self):
        mb = MotivationBlock(
            agent=AgentMotivationBlock(profile="linear"),
            governance=GovernanceMotivationBlock(
                model="hybrid", rho=0.6, kappa=5.0, budget_base=3
            ),
        )
        sos = SoSDefinition(name="t", motivation=mb)
        assert sos.motivation is not None
        assert sos.motivation.agent.profile == "linear"
        assert sos.motivation.governance.model == "hybrid"
        assert sos.motivation.governance.rho == 0.6

    def test_motivation_does_not_break_verify(self):
        sos = SoSDefinition(
            name="t",
            motivation=MotivationBlock(
                agent=AgentMotivationBlock(profile="polarized"),
                governance=GovernanceMotivationBlock(model="none"),
            ),
        )
        # Should run without raising; core verifier ignores the block.
        results = verify(sos)
        assert isinstance(results, list)


class TestSmtEntryContent:
    """An smt entry is checked for what the verifier can check about it."""

    def _sos(self):
        from cadl.parser import parse
        return parse(
            "sos:\n"
            '  name: "T"\n'
            "  type: Directed\n"
            "  actors:\n"
            "    - id: A\n"
            "      role: r\n"
            "  contracts:\n"
            "    - id: C1\n"
            "      parties: [A]\n"
            "  transitions:\n"
            "    - from: NORMAL\n"
            "      to: DEGRADED\n"
            '      condition: "load > 0.8"\n'
            "    - from: DEGRADED\n"
            "      to: NORMAL\n"
            '      condition: "load <= 0.8"\n'
        )

    def test_unknown_target_fails(self):
        spec = VerificationSpec(id="v", type="safety", target="DOES_NOT_EXIST")
        r = dispatch_spec(spec, self._sos())
        assert r.status == "failed" and "DOES_NOT_EXIST" in r.message

    def test_declared_targets_pass(self):
        for target in ("C1", "NORMAL", "NORMAL->DEGRADED"):
            spec = VerificationSpec(id="v", type="safety", target=target)
            assert dispatch_spec(spec, self._sos()).status == "passed", target

    def test_target_is_not_checked_without_a_definition(self):
        spec = VerificationSpec(id="v", type="safety", target="ANYTHING")
        assert dispatch_spec(spec).status == "passed"

    def test_unsatisfiable_expr_fails(self):
        for expr in ("false", "x > 5 AND x < 3"):
            spec = VerificationSpec(id="v", type="safety", expr=expr)
            assert dispatch_spec(spec).status == "failed", expr

    def test_satisfiable_expr_passes_and_says_it_is_not_a_proof(self):
        spec = VerificationSpec(id="v", type="safety", expr="x > 5")
        r = dispatch_spec(spec)
        assert r.status == "passed" and "not proved" in r.message

    def test_verify_passes_the_definition_through(self):
        sos = self._sos()
        sos.verifications.append(
            VerificationSpec(id="v_bad", type="safety", target="NOPE")
        )
        results = {r.check_name: r for r in verify(sos)}
        assert results["verification.v_bad"].status == "failed"
