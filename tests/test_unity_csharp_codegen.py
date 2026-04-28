"""Tests for the Unity C# codegen target (SoS-DSL extension).

These are content-level snapshot tests. They check that the emitter
produces the expected file set and that key invariants hold in the
generated C# (state enum membership, deadline arming, monitor wiring,
namespace boundaries). They do NOT compile the C# — that requires
`dotnet`, which is not part of the test environment.
"""

from pathlib import Path

import pytest

from cadl.parser import parse
from cadl.codegen import generate
from cadl.codegen.unity_csharp import generate_unity_csharp
from cadl.codegen.unity_csharp.contract_emitter import _csharp_class_name


EXAMPLE = (
    Path(__file__).parent.parent
    / "examples" / "sos_dsl_robot_delivery.cadl"
)


@pytest.fixture
def out_dir(tmp_path) -> Path:
    sos = parse(EXAMPLE.read_text(encoding="utf-8"))
    target = tmp_path / "unity"
    generate_unity_csharp(sos, target)
    return target


# ---------------------------------------------------------------------------
# File set
# ---------------------------------------------------------------------------

class TestFileSet:
    def test_runtime_files_exist(self, out_dir):
        for name in [
            "Severity.cs",
            "ContractEvent.cs",
            "EventBus.cs",
            "PredicateEvaluator.cs",
            "ContractRuntime.cs",
        ]:
            assert (out_dir / "Runtime" / name).is_file(), name

    def test_per_contract_files_exist(self, out_dir):
        cls = _csharp_class_name("DELIVERY_SLA")
        for kind in ("State", "Contract", "Monitors"):
            assert (out_dir / "Generated" / f"{cls}{kind}.cs").is_file(), kind

    def test_readme_exists(self, out_dir):
        assert (out_dir / "README.md").is_file()


class TestNaming:
    def test_class_name_pascal_case(self):
        assert _csharp_class_name("DELIVERY_SLA") == "DeliverySla"
        assert _csharp_class_name("delivery-sla") == "DeliverySla"
        assert _csharp_class_name("Delivery_Sla") == "DeliverySla"
        assert _csharp_class_name("") == "Contract"


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------

class TestStateEnum:
    def test_all_lifecycle_states_emitted(self, out_dir):
        text = (out_dir / "Generated" / "DeliverySlaState.cs").read_text()
        assert "public enum DeliverySlaState" in text
        for s in [
            "Proposed", "Assigned", "Accepted",
            "Delivering", "Completed", "Violated", "Terminated",
        ]:
            assert f"        {s}," in text, f"state {s} missing from enum"


# ---------------------------------------------------------------------------
# Contract class
# ---------------------------------------------------------------------------

class TestContractClass:
    def test_implements_icontractinstance(self, out_dir):
        text = (out_dir / "Generated" / "DeliverySlaContract.cs").read_text()
        assert "IContractInstance" in text
        assert "public string ContractId =>" in text
        assert "ContractId => \"DELIVERY_SLA\"" in text

    def test_initial_state_proposed(self, out_dir):
        text = (out_dir / "Generated" / "DeliverySlaContract.cs").read_text()
        # Constructor sets _state to Proposed
        assert "_state = DeliverySlaState.Proposed;" in text

    def test_assign_transition_emitted(self, out_dir):
        text = (out_dir / "Generated" / "DeliverySlaContract.cs").read_text()
        # The on: literal becomes a string compare; the to_state is
        # written as the typed enum value.
        assert (
            'ev.Name == "DISPATCHER -> ROBOT[i] : route_assignment"'
            in text
        )
        assert "transitionId: \"assign\"" in text
        assert "toState: DeliverySlaState.Assigned" in text

    def test_state_set_from_expands(self, out_dir):
        """`from: [Assigned, Accepted, Delivering]` expands to OR'd guards."""
        text = (out_dir / "Generated" / "DeliverySlaContract.cs").read_text()
        # All three from-states for late_failure show up in the same
        # transition's guard.
        late_idx = text.index("transitionId: \"late_failure\"")
        snippet = text[max(0, late_idx - 400):late_idx]
        assert "_state == DeliverySlaState.Assigned" in snippet
        assert "_state == DeliverySlaState.Accepted" in snippet
        assert "_state == DeliverySlaState.Delivering" in snippet

    def test_deadline_arming(self, out_dir):
        text = (out_dir / "Generated" / "DeliverySlaContract.cs").read_text()
        # Accept has deadline 5s -> 5000 ms; armed when entering Assigned;
        # severity Major; lift to Violated.
        assert "if (newState == DeliverySlaState.Assigned)" in text
        assert "deadlineMs: nowMs + 5000" in text
        assert "targetState: DeliverySlaState.Violated" in text
        assert "severity: Severity.Major" in text

    def test_terminal_states_close_instance(self, out_dir):
        text = (out_dir / "Generated" / "DeliverySlaContract.cs").read_text()
        # IsTerminal switch covers Completed, Violated, Terminated.
        for s in ("Completed", "Violated", "Terminated"):
            assert f"case DeliverySlaState.{s}: return true;" in text


# ---------------------------------------------------------------------------
# Monitors
# ---------------------------------------------------------------------------

class TestMonitors:
    def test_three_monitors_emitted(self, out_dir):
        text = (out_dir / "Generated" / "DeliverySlaMonitors.cs").read_text()
        for mid in ("battery_guard", "collision_watch", "deadline_watch"):
            assert f"private void Eval_{mid}(" in text

    def test_periodic_dispatch_with_correct_period(self, out_dir):
        text = (out_dir / "Generated" / "DeliverySlaMonitors.cs").read_text()
        # battery_guard: periodic 500ms
        assert "if (nowMs - _last_battery_guard >= 500)" in text
        # collision_watch: periodic 100ms
        assert "if (nowMs - _last_collision_watch >= 100)" in text
        # deadline_watch: periodic 1000ms
        assert "if (nowMs - _last_deadline_watch >= 1000)" in text

    def test_violation_severity(self, out_dir):
        text = (out_dir / "Generated" / "DeliverySlaMonitors.cs").read_text()
        # collision_watch is Critical; battery_guard is Major.
        assert "Severity = Severity.Critical" in text
        assert "Severity = Severity.Major" in text

    def test_state_jump_helper(self, out_dir):
        text = (out_dir / "Generated" / "DeliverySlaMonitors.cs").read_text()
        # The helper translates a target like "Violated" into a state
        # jump on the owner contract.
        assert "Enum.TryParse<DeliverySlaState>" in text
        assert "_owner.JumpToState" in text


# ---------------------------------------------------------------------------
# Predicate evaluator + runtime are namespace-clean
# ---------------------------------------------------------------------------

class TestRuntimeFiles:
    def test_namespace_uniform(self, out_dir):
        for f in (out_dir / "Runtime").glob("*.cs"):
            assert "namespace CADL.SosDsl" in f.read_text()
        for f in (out_dir / "Generated").glob("*.cs"):
            assert "namespace CADL.SosDsl" in f.read_text()

    def test_predicate_evaluator_has_required_ops(self, out_dir):
        text = (out_dir / "Runtime" / "PredicateEvaluator.cs").read_text()
        for op in ('"<="', '">="', '"=="', '"!="', '"AND"', '"OR"', '"NOT"', '"IN"'):
            assert op in text or op.replace('"', '') in text, op


# ---------------------------------------------------------------------------
# generate() top-level dispatch
# ---------------------------------------------------------------------------

class TestPublicGenerate:
    def test_dispatch_to_unity_csharp(self, tmp_path):
        sos = parse(EXAMPLE.read_text(encoding="utf-8"))
        target = tmp_path / "unity"
        generate(sos, target, target="unity-csharp")
        assert (target / "Runtime" / "ContractRuntime.cs").is_file()
        assert (target / "Generated" / "DeliverySlaContract.cs").is_file()


# ---------------------------------------------------------------------------
# Backward compat: a contract without lifecycle/monitors is skipped
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    def test_contract_without_extension_skipped(self, tmp_path):
        src = '''\
sos:
  name: Plain
  contracts:
    - id: PLAIN
      parties: [A]
      assume: ["A.alive == true"]
'''
        sos = parse(src)
        target = tmp_path / "unity"
        generate_unity_csharp(sos, target)
        # Runtime files still emit, but no per-contract files.
        assert (target / "Runtime" / "ContractRuntime.cs").is_file()
        assert not list((target / "Generated").glob("*.cs"))
