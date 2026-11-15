"""Regime bindings (`sos.regimes:`, Appendix A, A.7.1) and the transition
`safety_invariant` in the simulator IR (cadl 0.3.32)."""

from __future__ import annotations

import importlib
import io
import json
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from cadl.cli import _ir_to_dict, main
from cadl.parser import parse
from cadl.sim import generate_config, lower_to_ir
from cadl.type_checker import type_check
from cadl.verifier import verify

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

BASE = textwrap.dedent("""\
    sos:
      name: RobotTown
      type: Acknowledged
      context:
        environment:
          bays: 0
          edge_grant: 0.0
          exclusive_edges: false
          mode_name: "free"
      actors:
        - id: STEWARD
          role: steward
        - id: "ROBOT[1..3]"
          role: robot
      contracts:
        - id: ROUTING
          parties: [STEWARD, "ROBOT[*]"]
          guarantee:
            - "robots_on_road >= 0"
          authority:
            decision_holder: STEWARD
            beta: 0.2
        - id: BAY_ASSIGNMENT
          parties: [STEWARD, "ROBOT[*]"]
          guarantee:
            - "waiting_on_road == 0"
        - id: FREE_PASSAGE
          parties: ["ROBOT[*]"]
          guarantee:
            - "waiting_on_road >= 1"
      transitions:
        - from: DISTRIBUTED
          to: CENTRALIZED
          condition: "orders_in_window < 22"
          safety_invariant: "robots_on_road == 0"
        - from: CENTRALIZED
          to: DISTRIBUTED
          condition: "orders_in_window >= 26"
    """)

GOOD = BASE + textwrap.indent(textwrap.dedent("""\
      regimes:
        DISTRIBUTED:
          contracts:
            suspended: [BAY_ASSIGNMENT]
        CENTRALIZED:
          parameters:
            ROUTING.beta: 0.9
            environment.bays: 3
            environment.edge_grant: 1
            environment.exclusive_edges: true
            environment.mode_name: "steward"
          contracts:
            active: [ROUTING, BAY_ASSIGNMENT]
    """), "  ")


def _with_regimes(block: str) -> str:
    return BASE + textwrap.indent(textwrap.dedent(block), "  ")


def _errors(source: str) -> list[str]:
    return [e.message for e in type_check(parse(source)).errors]


def _ir(source: str) -> dict:
    return _ir_to_dict(lower_to_ir(parse(source)))


class TestSafetyInvariantInIR(unittest.TestCase):
    def test_emitted_as_written(self):
        d = _ir(BASE)
        self.assertEqual(d["transitions"][0]["safety_invariant"], "robots_on_road == 0")

    def test_absent_key_when_none(self):
        d = _ir(BASE)
        self.assertNotIn("safety_invariant", d["transitions"][1])

    def test_example_files(self):
        for name in ("robot_delivery.cadl", "smart_city_traffic.cadl", "supply_chain.cadl"):
            path = EXAMPLES / name
            if not path.exists():
                continue
            sos = parse(path.read_text(encoding="utf-8"))
            d = _ir_to_dict(lower_to_ir(sos))
            for t, src in zip(d["transitions"], sos.transitions):
                self.assertEqual(t.get("safety_invariant"), src.safety_invariant)

    def test_cli_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "m.cadl"
            p.write_text(BASE, encoding="utf-8")
            out = io.StringIO()
            argv = sys.argv
            sys.argv = ["cadl", "sim-ir", str(p), "--format", "json"]
            try:
                with redirect_stdout(out):
                    rc = main()
            finally:
                sys.argv = argv
            self.assertEqual(rc, 0)
            d = json.loads(out.getvalue())
            self.assertEqual(d["transitions"][0]["safety_invariant"], "robots_on_road == 0")
            self.assertNotIn("regimes", d)

    def test_sim_gen_targets(self):
        ir = lower_to_ir(parse(BASE))
        self.assertIn("safety_invariant: robots_on_road == 0", generate_config(ir, "python"))
        self.assertIn('"safety_invariant": "robots_on_road == 0"', generate_config(ir, "go"))
        self.assertIn('"safetyInvariant": "robots_on_road == 0"', generate_config(ir, "unity"))


class TestRegimeBindingsCheck(unittest.TestCase):
    def test_good_passes(self):
        self.assertEqual(_errors(GOOD), [])

    def test_without_block_unchanged(self):
        sos = parse(BASE)
        self.assertIsNone(sos.regimes_raw)
        self.assertNotIn("regimes", _ir(BASE))

    def _one(self, block: str, *needles: str) -> None:
        errs = _errors(_with_regimes(block))
        self.assertTrue(errs, "expected an error")
        joined = "\n".join(errs)
        for n in needles:
            self.assertIn(n, joined)

    def test_unknown_regime(self):
        self._one("regimes:\n  PEAK: {}\n", "'PEAK', which is not a regime", "(R-1)")

    def test_boolean_regime_key(self):
        self._one("regimes:\n  yes: {}\n", "a boolean or null", "(R-1)")

    def test_no_transitions(self):
        src = BASE.split("  transitions:")[0] + "  regimes:\n    A: {}\n"
        self.assertIn("has no transitions", "\n".join(_errors(src)))

    def test_unknown_entry_key(self):
        self._one("regimes:\n  CENTRALIZED: { extra: 1 }\n", "only 'parameters' and 'contracts'",
                  "(R-2)")

    def test_unknown_parameter_forms(self):
        self._one("regimes:\n  CENTRALIZED: { parameters: { foo: 1 } }\n", "(R-3)")
        self._one("regimes:\n  CENTRALIZED: { parameters: { ROUTING.gamma: 0.1 } }\n",
                  "alpha, beta or lambda")
        self._one("regimes:\n  CENTRALIZED: { parameters: { NOPE.beta: 0.1 } }\n",
                  "'NOPE' is not a declared contract")
        self._one("regimes:\n  CENTRALIZED: { parameters: { environment.nope: 1 } }\n",
                  "has no entry 'nope'")

    def test_parameter_not_declared_by_contract(self):
        self._one("regimes:\n  CENTRALIZED: { parameters: { BAY_ASSIGNMENT.beta: 0.4 } }\n",
                  "does not write authority.beta")

    def test_institutional_range_and_type(self):
        self._one("regimes:\n  CENTRALIZED: { parameters: { ROUTING.beta: 1.5 } }\n",
                  "outside [0, 1]", "(R-4)")
        self._one("regimes:\n  CENTRALIZED: { parameters: { ROUTING.beta: high } }\n",
                  "not a number")
        self._one("regimes:\n  CENTRALIZED: { parameters: { ROUTING.beta: true } }\n",
                  "not a number")

    def test_environment_types(self):
        self._one("regimes:\n  CENTRALIZED: { parameters: { environment.bays: 2.5 } }\n",
                  "must be an integer")
        self._one("regimes:\n  CENTRALIZED: { parameters: { environment.exclusive_edges: 1 } }\n",
                  "must be a boolean")
        self._one("regimes:\n  CENTRALIZED: { parameters: { environment.mode_name: 3 } }\n",
                  "must be a string")
        # an integer is accepted for a number
        self.assertEqual(_errors(_with_regimes(
            "regimes:\n  CENTRALIZED: { parameters: { environment.edge_grant: 2 } }\n")), [])

    def test_contract_lists(self):
        self._one("regimes:\n  CENTRALIZED: { contracts: { active: [GHOST] } }\n",
                  "'GHOST', which is not a declared contract", "(R-5)")
        self._one("regimes:\n  CENTRALIZED: { contracts: { suspended: [ROUTING, ROUTING] } }\n",
                  "lists 'ROUTING' twice")
        self._one("regimes:\n  CENTRALIZED: { contracts: { active: [ROUTING], "
                  "suspended: [FREE_PASSAGE] } }\n", "gives both 'active' and 'suspended'")
        self._one("regimes:\n  CENTRALIZED: { contracts: { paused: [ROUTING] } }\n", "(R-5)")
        self._one("regimes:\n  CENTRALIZED: { contracts: {} }\n", "one of the keys", "(R-5)")
        self._one("regimes:\n  CENTRALIZED: { contracts: null }\n", "one of the keys", "(R-5)")

    def test_conflicts(self):
        self._one("regimes:\n  CENTRALIZED:\n    parameters: { ROUTING.beta: 0.7 }\n"
                  "    contracts: { suspended: [ROUTING] }\n",
                  "is suspended in the regime 'CENTRALIZED'", "(R-6)")
        self._one("regimes:\n  CENTRALIZED:\n    parameters: { ROUTING.beta: 0.7, "
                  "\"ROUTING . beta\": 0.3 }\n", "binds 'ROUTING.beta' twice", "(R-6)")


class TestRegimeBindingsUse(unittest.TestCase):
    def test_ir(self):
        d = _ir(GOOD)
        self.assertEqual(d["regimes"], [
            {"name": "DISTRIBUTED", "parameters": {},
             "active_contracts": ["ROUTING", "FREE_PASSAGE"],
             "suspended_contracts": ["BAY_ASSIGNMENT"]},
            {"name": "CENTRALIZED",
             "parameters": {"ROUTING.beta": 0.9, "environment.bays": 3,
                            "environment.edge_grant": 1,
                            "environment.exclusive_edges": True,
                            "environment.mode_name": "steward"},
             "active_contracts": ["ROUTING", "BAY_ASSIGNMENT"],
             "suspended_contracts": ["FREE_PASSAGE"]},
        ])
        # the contracts keep their declared values
        self.assertEqual(d["institution"]["contracts"][0]["governance"]["beta"], 0.2)

    def test_ir_null_when_broken(self):
        d = _ir(_with_regimes("regimes:\n  PEAK: {}\n"))
        self.assertIn("regimes", d)
        self.assertIsNone(d["regimes"])

    def test_sim_gen(self):
        ir = lower_to_ir(parse(GOOD))
        py = generate_config(ir, "python")
        self.assertIn("regimes:", py)
        self.assertIn("ROUTING.beta: 0.9", py)
        go = json.loads(generate_config(ir, "go"))
        self.assertEqual(go["regimes"][1]["suspended_contracts"], ["FREE_PASSAGE"])
        unity = json.loads(generate_config(ir, "unity"))
        self.assertEqual(unity["regimeBindings"][0]["suspendedContracts"], ["BAY_ASSIGNMENT"])
        # without the block the configurations have no such key
        plain = lower_to_ir(parse(BASE))
        self.assertNotIn("regimes", json.loads(generate_config(plain, "go")))
        self.assertNotIn("regimeBindings", json.loads(generate_config(plain, "unity")))

    def test_verify_warns_for_pairs_apart_under_bindings(self):
        results = {r.check_name: r for r in verify(parse(GOOD))}
        pair = results["Cross-contract 'BAY_ASSIGNMENT' x 'FREE_PASSAGE'"]
        self.assertEqual(pair.status, "warning")
        self.assertIn("under the regime bindings of A.7.1", pair.message)
        self.assertIn("'BAY_ASSIGNMENT' is in force in CENTRALIZED", pair.message)
        self.assertIn("'FREE_PASSAGE' in DISTRIBUTED", pair.message)
        self.assertIn("[WARN]", str(pair))
        self.assertEqual(results["Cross-contract 'ROUTING' x 'BAY_ASSIGNMENT'"].status, "passed")
        # without the block the contradictory pair is checked and fails
        plain = {r.check_name: r for r in verify(parse(BASE))}
        self.assertEqual(plain["Cross-contract 'BAY_ASSIGNMENT' x 'FREE_PASSAGE'"].status,
                         "failed")

    def test_verify_info_for_consistent_pair_apart(self):
        src = GOOD.replace('"waiting_on_road >= 1"', '"waiting_on_road >= 0"')
        results = {r.check_name: r for r in verify(parse(src))}
        pair = results["Cross-contract 'BAY_ASSIGNMENT' x 'FREE_PASSAGE'"]
        self.assertEqual(pair.status, "info")
        self.assertIn("jointly satisfiable", pair.message)
        self.assertIn("under the regime bindings of A.7.1", pair.message)

    def test_verify_empty_block_without_transitions_checks_all_pairs(self):
        src = textwrap.dedent("""\
            sos:
              name: Empty
              actors:
                - { id: A, role: a }
                - { id: B, role: b }
              contracts:
                - { id: C1, parties: [A, B], guarantee: ["x >= 5"] }
                - { id: C2, parties: [A, B], guarantee: ["x <= 2"] }
              regimes: {}
            """)
        self.assertEqual(_errors(src), [])
        results = {r.check_name: r for r in verify(parse(src))}
        self.assertEqual(results["Cross-contract 'C1' x 'C2'"].status, "failed")

    def test_verify_checks_pair_active_together(self):
        src = _with_regimes("regimes:\n  CENTRALIZED: { contracts: { suspended: [ROUTING] } }\n")
        results = {r.check_name: r for r in verify(parse(src))}
        self.assertEqual(results["Cross-contract 'BAY_ASSIGNMENT' x 'FREE_PASSAGE'"].status,
                         "failed")

    def test_codegen(self):
        from cadl.codegen import generate
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "town_rt"
            generate(parse(GOOD), pkg)
            sys.path.insert(0, tmp)
            try:
                mod = importlib.import_module("town_rt.runtime")
                cls = next(getattr(mod, n) for n in dir(mod) if n.endswith("Runtime"))
                rt = cls()
                self.assertEqual(rt.regime_controller.current_regime, "DISTRIBUTED")
                self.assertEqual(rt.ctx.parameters["ROUTING.beta"], 0.2)
                rt.ctx.state.update(waiting_on_road=0, robots_on_road=0)
                names = [v.contract_id for v in rt.run_monitor_cycle()]
                self.assertEqual(names, ["FREE_PASSAGE"])
                rt.regime_controller.execute_transition("CENTRALIZED", rt.ctx)
                self.assertEqual(rt.ctx.parameters["ROUTING.beta"], 0.9)
                self.assertEqual(rt.ctx.parameters["environment.bays"], 3)
                self.assertEqual(rt.run_monitor_cycle(), [])
            finally:
                sys.path.remove(tmp)
                for name in [m for m in sys.modules if m.startswith("town_rt")]:
                    del sys.modules[name]

    def test_codegen_without_block_unchanged(self):
        from cadl.codegen.runtime_gen import generate_runtime_module
        from cadl.codegen.transition_gen import generate_transitions_module
        sos = parse(BASE)
        self.assertNotIn("contract_active", generate_runtime_module(sos))
        self.assertNotIn("apply_bindings", generate_transitions_module(sos.transitions))


class TestRegimeBindingsAndStochasticModel(unittest.TestCase):
    """R-7: the core binding is the value of a bound model parameter under
    the design of the regime; model.regimes may repeat it, not contradict it."""

    def setUp(self):
        self.src = (EXAMPLES / "stochastic_robot_delivery.cadl").read_text(encoding="utf-8")

    def _variant(self, core: str, drop_model_regimes: bool = False) -> str:
        src = self.src.replace("\n  model:\n", "\n" + textwrap.indent(core, "  ") + "\n  model:\n")
        if drop_model_regimes:
            src = src.replace(
                "    regimes:\n      CENTRALIZED: { parameters: { beta: 0.9 } }\n"
                "      DISTRIBUTED: { parameters: { beta: 0.1 } }\n", "")
        return src

    def _designs(self, src: str):
        from cadl.stochastic import checked_model
        model = checked_model(parse(src))
        return None if model is None else {d.name: dict(d.assign) for d in model.designs}

    def test_core_binding_replaces_model_regimes(self):
        core = ("regimes:\n  CENTRALIZED: { parameters: { DELIVERY.beta: 0.9 } }\n"
                "  DISTRIBUTED: { parameters: { DELIVERY.beta: 0.1 } }\n")
        a = self._designs(self.src)
        b = self._designs(self._variant(core, drop_model_regimes=True))
        c = self._designs(self._variant(core))
        self.assertIsNotNone(a)
        self.assertEqual(a, b)
        self.assertEqual(a, c)

    def test_conflict_is_error(self):
        src = self._variant("regimes:\n  DISTRIBUTED: { parameters: { DELIVERY.beta: 0.2 } }\n")
        errs = "\n".join(_errors(src))
        self.assertIn("(R-7)", errs)
        self.assertIsNone(self._designs(src))

    def test_broken_block_blocks_designs(self):
        src = self._variant("regimes:\n  PEAK: {}\n")
        self.assertIsNone(self._designs(src))


if __name__ == "__main__":
    unittest.main()
