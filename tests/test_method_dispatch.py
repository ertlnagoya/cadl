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
