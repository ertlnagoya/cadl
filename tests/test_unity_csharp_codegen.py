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

    def test_predicate_evaluator_reads_negative_numbers(self, out_dir, tmp_path):
        # Compiled with Mono when it is installed; the CI runners skip it.
        import shutil
        import subprocess
        if not (shutil.which("mcs") and shutil.which("mono")):
            pytest.skip("Mono (mcs, mono) is not installed")
        main = tmp_path / "Main.cs"
        main.write_text("""
using System; using System.Collections.Generic; using CADL.SosDsl;
class M { static void Main() {
  var w = new Dictionary<string, object>{{"p", -2L}, {"x", 3.0}};
  foreach (var e in new[]{"p == -2", "p < -1", "x > -0.5", "p IN [-2, 1]",
                          "p == 2", "x -1", "x-1 > 0"})
    Console.WriteLine(PredicateEvaluator.Evaluate(e, w));
}}
""")
        exe = tmp_path / "m.exe"
        subprocess.run(["mcs", "-nologo", f"-out:{exe}", str(main),
                        str(out_dir / "Runtime" / "PredicateEvaluator.cs")], check=True)
        out = subprocess.run(["mono", str(exe)], check=True, capture_output=True,
                             text=True).stdout.split()
        # A minus right after an operand is a subtraction, which the
        # evaluator does not support: the expression is false, as before.
        assert out == ["True", "True", "True", "True", "False", "False", "False"]


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


class TestTargetedPost:
    """PostEvent can address one instance, like post_event(instance_id=...)
    in the Python reference runtime. Without it, a host that drives one
    instance per robot advances every robot's contract on one robot's event."""

    def test_post_event_takes_an_instance_id(self, out_dir):
        src = (out_dir / "Runtime" / "ContractRuntime.cs").read_text(encoding="utf-8")
        assert "string instanceId = null)" in src

    def test_targeted_post_reaches_only_that_instance(self, out_dir):
        src = (out_dir / "Runtime" / "ContractRuntime.cs").read_text(encoding="utf-8")
        body = src[src.index("public void PostEvent"):src.index("public void Tick")]
        assert "_instances.TryGetValue(instanceId, out var target)" in body
        targeted = body.index("if (instanceId != null)")
        broadcast = body.index("foreach (var inst in _instances.Values)")
        assert targeted < broadcast
        assert "return;" in body[targeted:broadcast]


def _run_csharp(tmp_path, src, contract_cls, events, horizon):
    """Generate the Unity C# of src, compile it with Mono's csc, open one
    instance of contract_cls, post events [(t, name)] and tick every 1 ms up
    to horizon. Returns the lifecycle and violation records as lines."""
    import shutil
    import subprocess
    if not (shutil.which("csc") and shutil.which("mono")):
        pytest.skip("Mono (csc, mono) is not installed")
    target = tmp_path / "unity"
    generate_unity_csharp(parse(src), target)
    posts = "\n".join(f'    if (t == {t}) rt.PostEvent("{n}", t);' for t, n in events)
    main = tmp_path / "Main.cs"
    main.write_text("""
using System; using System.Collections.Generic; using CADL.SosDsl;
class M { static void Main() {
  var rt = new ContractRuntime();
  rt.Events.Subscribe(e => {
    if (e.Kind == ContractEventKind.Lifecycle && e.Cause == "open") return;
    Console.WriteLine(e.TMs + " " + e.Kind + " " + e.FromState + " " + e.ToState);
  });
  rt.Register(new %s("i1", rt, 0));
  for (long t = 0; t <= %d; t++) {
%s
    rt.Tick(t);
  }
}}
""" % (contract_cls, horizon, posts))
    exe = tmp_path / "m.exe"
    subprocess.run(["csc", "-nologo", "-nowarn:219,162,168", f"-out:{exe}", str(main),
                    *[str(p) for p in target.rglob("*.cs")]], check=True)
    return subprocess.run(["mono", str(exe)], check=True, capture_output=True,
                          text=True).stdout.splitlines()


class TestDeadlineScope:
    """A deadline is scoped to the from states of its transition (L-4) and
    restarts on every entry into one of them, a self-loop included (#91)."""

    SRC = '''\
sos:
  name: "Repro2"
  type: Collaborative
  version: "0.1.0"
  extensions:
    - sos-dsl: 0.1
  actors:
    - { id: A, role: "a", autonomy: high }
    - { id: B, role: "b", autonomy: high }
  contracts:
    - id: C2
      parties: [A, B]
      assume: ["A.x >= 0"]
      guarantee: ["B.y >= 0"]
      lifecycle:
        states: [Idle, Waiting, Done, Failed]
        initial: Idle
        terminal: [Done, Failed]
        transitions:
          - { id: request, from: Idle, to: Waiting, on: "A -> B : req" }
          - id: answer
            from: Waiting
            to: Done
            on: "B -> A : ans"
            deadline: 5s
            on_violation: { transition: Failed, severity: Major }
          - { id: withdraw, from: Waiting, to: Idle, on: "A -> B : cancel" }
          - { id: ping, from: Waiting, to: Waiting, on: "A -> B : ping" }
'''

    def test_clears_all_deadlines_on_a_transition(self, out_dir):
        text = (out_dir / "Generated" / "DeliverySlaContract.cs").read_text()
        body = text[text.index("public void FireTransition"):text.index("public void JumpToState")]
        assert "_deadlines[i].TransitionId == transitionId" not in body
        assert body.index("_deadlines.Clear();") < body.index("rt.Emit(")

    def test_leaving_the_from_state_cancels_the_deadline(self, tmp_path):
        out = _run_csharp(tmp_path, self.SRC, "C2Contract", [(1000, "A -> B : req"), (2000, "A -> B : cancel")], 10000)
        assert out == ["1000 Lifecycle Idle Waiting", "2000 Lifecycle Waiting Idle"], out

    def test_self_loop_restarts_the_deadline(self, tmp_path):
        out = _run_csharp(tmp_path, self.SRC, "C2Contract", [(1000, "A -> B : req"), (4000, "A -> B : ping")], 10000)
        assert out == ["1000 Lifecycle Idle Waiting", "4000 Lifecycle Waiting Waiting",
                       "9000 Violation  ", "9000 Lifecycle Waiting Failed"], out


class TestTwoDeadlinesAtOnce:
    """Two deadlines of one state expire in the same tick: the first forced
    move ends the other one (it used to index past the rebuilt list)."""

    SRC = TestDeadlineScope.SRC.replace(
        "        states: [Idle, Waiting, Done, Failed]",
        "        states: [Idle, Waiting, Late, Done, Failed]").replace(
        "          - { id: ping, from: Waiting, to: Waiting, on: \"A -> B : ping\" }",
        "          - id: ack\n"
        "            from: Waiting\n"
        "            to: Waiting\n"
        "            on: \"B -> A : ack\"\n"
        "            deadline: 5s\n"
        "            on_violation: { transition: Late, severity: Minor }\n"
        "          - { id: late_done, from: Late, to: Done, on: \"B -> A : ans\" }")

    def test_one_forced_move(self, tmp_path):
        out = _run_csharp(tmp_path, self.SRC, "C2Contract", [(1000, "A -> B : req")], 10000)
        assert out[0] == "1000 Lifecycle Idle Waiting", out
        assert len(out) == 3, out
        assert out[1] == "6000 Violation  " and out[2].startswith("6000 Lifecycle Waiting "), out


class TestMonitorAfterClose:
    """A monitor that moves the instance to a terminal state closes it; the
    other monitors of the same tick are not evaluated any more (#90)."""

    SRC = '''\
sos:
  name: "Repro1"
  type: Collaborative
  version: "0.1.0"
  extensions:
    - sos-dsl: 0.1
  actors:
    - { id: A, role: "a", autonomy: high }
    - { id: B, role: "b", autonomy: high }
  contracts:
    - id: C1
      parties: [A, B]
      assume: ["A.x >= 0"]
      guarantee: ["B.y >= 0"]
      lifecycle:
        states: [Open, Failed]
        initial: Open
        terminal: [Failed]
      monitors:
        - id: m_first
          observe: ["A.x"]
          sampling: periodic(1s)
          rule: "A.x > 5"
          on_match: { transition: Failed, severity: Major }
        - id: m_second
          observe: ["A.x"]
          sampling: periodic(1s)
          rule: "A.x > 3"
          on_match: { transition: Failed, severity: Critical }
'''

    def _generate(self, tmp_path):
        target = tmp_path / "unity"
        generate_unity_csharp(parse(self.SRC), target)
        return target

    def test_each_monitor_checks_closed(self, tmp_path):
        text = (self._generate(tmp_path) / "Generated" / "C1Monitors.cs").read_text()
        assert text.count("if (_owner.Closed) return;") == 2

    def test_second_monitor_not_evaluated_after_close(self, tmp_path):
        # Compiled with Mono's Roslyn csc when it is installed (mcs lacks
        # `in` parameters); the CI runners skip it.
        import shutil
        import subprocess
        if not (shutil.which("csc") and shutil.which("mono")):
            pytest.skip("Mono (csc, mono) is not installed")
        target = self._generate(tmp_path)
        main = tmp_path / "Main.cs"
        main.write_text("""
using System; using System.Collections.Generic; using CADL.SosDsl;
class M { static void Main() {
  var rt = new ContractRuntime();
  rt.Events.Subscribe(e => {
    if (e.Kind == ContractEventKind.Lifecycle && e.Cause == "open") return;
    Console.WriteLine(e.TMs + " " + e.Kind + " " + e.DetectedBy + " " + e.FromState + " " + e.ToState);
  });
  rt.Register(new C1Contract("c1", rt, 0));
  var a = new Dictionary<string, object>{{"x", 0L}};
  rt.UpdateWorld(new Dictionary<string, object>{{"A", a}});
  for (long t = 0; t <= 3000; t++) {
    if (t == 1500) { a["x"] = 10L; rt.UpdateWorld(new Dictionary<string, object>{{"A", a}}); }
    rt.Tick(t);
  }
}}
""")
        exe = tmp_path / "m.exe"
        sources = [str(p) for p in target.rglob("*.cs")]
        subprocess.run(["csc", "-nologo", "-nowarn:219,162,168", f"-out:{exe}", str(main), *sources],
                       check=True)
        out = subprocess.run(["mono", str(exe)], check=True, capture_output=True,
                             text=True).stdout.splitlines()
        assert out == ["2000 Violation monitor:m_first  ",
                       "2000 Lifecycle  Open Failed"], out
