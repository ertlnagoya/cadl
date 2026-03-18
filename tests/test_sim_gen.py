"""Tests for the simulator config generators and CLI commands."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import yaml

from cadl.parser import parse_file
from cadl.sim import generate_config, lower_to_ir, validate_ir
from cadl.sim.gen_go import generate_go_config
from cadl.sim.gen_python import generate_python_config
from cadl.sim.gen_unity import generate_unity_config

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


class TestPythonGenerator(unittest.TestCase):
    """Test Python simulator YAML config generation."""

    @classmethod
    def setUpClass(cls):
        sos = parse_file(EXAMPLES / "a_sos_robot_delivery.cadl")
        cls.ir = lower_to_ir(sos)
        cls.config_str = generate_python_config(cls.ir)
        cls.config = yaml.safe_load(cls.config_str)

    def test_valid_yaml(self):
        self.assertIsInstance(self.config, dict)

    def test_simulator_section(self):
        sim = self.config["simulator"]
        self.assertEqual(sim["name"], "MAPFRobotDelivery")
        self.assertEqual(sim["type"], "acknowledged")

    def test_agents(self):
        agents = self.config["agents"]
        self.assertEqual(len(agents), 3)
        templates = [a["template"] for a in agents]
        self.assertIn("DISPATCHER", templates)
        self.assertIn("ROBOT", templates)

    def test_planner_binding(self):
        dispatcher = [a for a in self.config["agents"]
                      if a["template"] == "DISPATCHER"][0]
        self.assertEqual(dispatcher["planner"]["type"], "central")
        self.assertEqual(dispatcher["planner"]["algorithm"], "ECBS")

    def test_communication_channels(self):
        channels = self.config.get("communication", {}).get("channels", [])
        self.assertTrue(len(channels) > 0)

    def test_governance(self):
        gov = self.config.get("governance", {})
        self.assertIn("decision_holders", gov)
        self.assertIn("authority_centralization", gov)

    def test_contracts(self):
        contracts = self.config["contracts"]
        self.assertEqual(len(contracts), 2)

    def test_protocols(self):
        protocols = self.config["protocols"]
        self.assertTrue(len(protocols) >= 3)

    def test_transitions(self):
        transitions = self.config.get("transitions", [])
        self.assertEqual(len(transitions), 4)

    def test_metrics(self):
        metrics = self.config.get("metrics", [])
        self.assertEqual(len(metrics), 4)


class TestUnityGenerator(unittest.TestCase):
    """Test Unity simulator JSON config generation."""

    @classmethod
    def setUpClass(cls):
        sos = parse_file(EXAMPLES / "c_sos_taxi_fleet.cadl")
        cls.ir = lower_to_ir(sos)
        cls.config_str = generate_unity_config(cls.ir)
        cls.config = json.loads(cls.config_str)

    def test_valid_json(self):
        self.assertIsInstance(self.config, dict)

    def test_simulator_config(self):
        sc = self.config["simulatorConfig"]
        self.assertEqual(sc["name"], "AutonomousTaxiFleet")
        self.assertEqual(sc["sosType"], "collaborative")

    def test_agent_templates(self):
        templates = self.config["agentTemplates"]
        self.assertEqual(len(templates), 3)
        taxi = [t for t in templates if t["templateId"] == "TAXI"][0]
        self.assertEqual(taxi["prefab"], "Agent_HighAutonomy")

    def test_camel_case_keys(self):
        self.assertIn("simulatorConfig", self.config)
        self.assertIn("agentTemplates", self.config)
        self.assertIn("communicationSetup", self.config)
        if self.config.get("regimeTransitions"):
            t = self.config["regimeTransitions"][0]
            self.assertIn("fromRegime", t)

    def test_protocols(self):
        protocols = self.config["protocols"]
        self.assertTrue(len(protocols) >= 3)
        self.assertIn("protocolId", protocols[0])

    def test_regime_transitions(self):
        transitions = self.config.get("regimeTransitions", [])
        self.assertEqual(len(transitions), 5)


class TestGoGenerator(unittest.TestCase):
    """Test Go simulator JSON config generation."""

    @classmethod
    def setUpClass(cls):
        sos = parse_file(EXAMPLES / "a_sos_robot_delivery.cadl")
        cls.ir = lower_to_ir(sos)
        cls.config_str = generate_go_config(cls.ir)
        cls.config = json.loads(cls.config_str)

    def test_valid_json(self):
        self.assertIsInstance(self.config, dict)

    def test_snake_case_keys(self):
        self.assertIn("sos_type", self.config)

    def test_agents(self):
        agents = self.config["agents"]
        self.assertEqual(len(agents), 3)
        dispatcher = [a for a in agents if a["id"] == "DISPATCHER"][0]
        self.assertIn("algorithm", dispatcher)
        self.assertEqual(dispatcher["algorithm"]["central"], "ECBS")

    def test_contracts_with_governance(self):
        contracts = self.config["contracts"]
        sla = [c for c in contracts if c["id"] == "DELIVERY_SLA"][0]
        self.assertIn("governance", sla)
        self.assertEqual(sla["governance"]["beta"], 0.8)

    def test_protocols(self):
        protocols = self.config["protocols"]
        self.assertTrue(len(protocols) >= 3)

    def test_transitions(self):
        transitions = self.config.get("transitions", [])
        self.assertEqual(len(transitions), 4)
        self.assertIn("from_regime", transitions[0])


class TestGenerateConfigDispatch(unittest.TestCase):
    """Test the generate_config() dispatch function."""

    @classmethod
    def setUpClass(cls):
        sos = parse_file(EXAMPLES / "a_sos_robot_delivery.cadl")
        cls.ir = lower_to_ir(sos)

    def test_python_target(self):
        result = generate_config(self.ir, "python")
        config = yaml.safe_load(result)
        self.assertEqual(config["simulator"]["name"], "MAPFRobotDelivery")

    def test_unity_target(self):
        result = generate_config(self.ir, "unity")
        config = json.loads(result)
        self.assertIn("agentTemplates", config)

    def test_go_target(self):
        result = generate_config(self.ir, "go")
        config = json.loads(result)
        self.assertIn("sos_type", config)

    def test_unknown_target(self):
        with self.assertRaises(ValueError):
            generate_config(self.ir, "rust")


class TestIntegration(unittest.TestCase):
    """End-to-end: parse → lower → validate → generate for all examples."""

    def _run_pipeline(self, cadl_file: Path, target: str):
        sos = parse_file(cadl_file)
        ir = lower_to_ir(sos)
        errors = validate_ir(ir)
        self.assertEqual(errors, [], f"{cadl_file.name} validation: {errors}")
        config = generate_config(ir, target)
        self.assertTrue(len(config) > 0)
        return config

    def test_a_sos_all_targets(self):
        f = EXAMPLES / "a_sos_robot_delivery.cadl"
        for target in ("python", "unity", "go"):
            with self.subTest(target=target):
                self._run_pipeline(f, target)

    def test_c_sos_all_targets(self):
        f = EXAMPLES / "c_sos_taxi_fleet.cadl"
        for target in ("python", "unity", "go"):
            with self.subTest(target=target):
                self._run_pipeline(f, target)

    def test_existing_robot_delivery(self):
        f = EXAMPLES / "robot_delivery.cadl"
        for target in ("python", "unity", "go"):
            with self.subTest(target=target):
                self._run_pipeline(f, target)

    def test_existing_household(self):
        f = EXAMPLES / "household_chores.cadl"
        for target in ("python", "unity", "go"):
            with self.subTest(target=target):
                self._run_pipeline(f, target)


class TestCLI(unittest.TestCase):
    """Test CLI subcommands."""

    def test_sim_validate(self):
        from cadl.cli import main
        ret = main(["sim-validate", str(EXAMPLES / "a_sos_robot_delivery.cadl")])
        self.assertEqual(ret, 0)

    def test_sim_ir_yaml(self):
        from cadl.cli import main
        ret = main(["sim-ir", str(EXAMPLES / "a_sos_robot_delivery.cadl")])
        self.assertEqual(ret, 0)

    def test_sim_ir_json(self):
        from cadl.cli import main
        ret = main(["sim-ir", str(EXAMPLES / "c_sos_taxi_fleet.cadl"),
                     "--format", "json"])
        self.assertEqual(ret, 0)

    def test_sim_gen_python(self):
        from cadl.cli import main
        ret = main(["sim-gen", str(EXAMPLES / "a_sos_robot_delivery.cadl"),
                     "--target", "python"])
        self.assertEqual(ret, 0)

    def test_sim_gen_unity(self):
        from cadl.cli import main
        ret = main(["sim-gen", str(EXAMPLES / "c_sos_taxi_fleet.cadl"),
                     "--target", "unity"])
        self.assertEqual(ret, 0)

    def test_sim_gen_go(self):
        from cadl.cli import main
        ret = main(["sim-gen", str(EXAMPLES / "a_sos_robot_delivery.cadl"),
                     "--target", "go"])
        self.assertEqual(ret, 0)

    def test_sim_validate_missing_file(self):
        from cadl.cli import main
        ret = main(["sim-validate", "nonexistent.cadl"])
        self.assertEqual(ret, 1)


if __name__ == "__main__":
    unittest.main()
