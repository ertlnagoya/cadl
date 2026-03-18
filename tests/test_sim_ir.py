"""Tests for the 3-layer Simulator IR: construction, lowering, and validation."""

from __future__ import annotations

import unittest
from pathlib import Path

from cadl.parser import parse_file
from cadl.sim.ir import (
    ActorSpec,
    AlgorithmLayer,
    AlgorithmSpec,
    ContractSpec,
    GovernanceParams,
    InstitutionLayer,
    MetricSpec,
    ProtocolLayer,
    ProtocolSpec,
    SimIR,
    StepSpec,
    TransitionSpec,
)
from cadl.sim.lower import lower_to_ir
from cadl.sim.validate import validate_ir

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


class TestIRConstruction(unittest.TestCase):
    """Test IR dataclass construction."""

    def test_empty_ir(self):
        ir = SimIR(name="test")
        self.assertEqual(ir.name, "test")
        self.assertEqual(ir.institution.actors, [])
        self.assertEqual(ir.protocol.protocols, [])
        self.assertEqual(ir.algorithm.algorithms, [])

    def test_actor_spec(self):
        a = ActorSpec(id="ROBOT", role="agent", autonomy="high", count=10)
        self.assertEqual(a.id, "ROBOT")
        self.assertEqual(a.count, 10)

    def test_governance_params(self):
        g = GovernanceParams(alpha=0.8, beta=0.2, lambda_=0.5,
                             decision_holder="DISPATCHER")
        self.assertEqual(g.alpha, 0.8)
        self.assertEqual(g.decision_holder, "DISPATCHER")

    def test_step_spec(self):
        s = StepSpec(type="message", sender="A", receiver="B", content="msg")
        self.assertEqual(s.type, "message")
        self.assertEqual(s.sender, "A")

    def test_full_ir_assembly(self):
        ir = SimIR(
            name="Test",
            sos_type="Acknowledged",
            institution=InstitutionLayer(
                actors=[ActorSpec(id="A", role="r", autonomy="high")],
                contracts=[ContractSpec(id="C1", parties=["A"])],
            ),
            protocol=ProtocolLayer(
                protocols=[ProtocolSpec(id="P1", trigger="event")],
                events=["event"],
            ),
            algorithm=AlgorithmLayer(
                algorithms=[AlgorithmSpec(name="routing", central="CBS", local="A*")],
            ),
        )
        self.assertEqual(len(ir.institution.actors), 1)
        self.assertEqual(len(ir.protocol.events), 1)
        self.assertEqual(ir.algorithm.algorithms[0].central, "CBS")


class TestLowerASoS(unittest.TestCase):
    """Test lowering the A-SoS (robot delivery) sample."""

    @classmethod
    def setUpClass(cls):
        cls.sos = parse_file(EXAMPLES / "a_sos_robot_delivery.cadl")
        cls.ir = lower_to_ir(cls.sos)

    def test_name_and_type(self):
        self.assertEqual(self.ir.name, "MAPFRobotDelivery")
        self.assertEqual(self.ir.sos_type, "Acknowledged")

    def test_environment(self):
        env = self.ir.environment
        self.assertEqual(env["grid_size"], 30)
        self.assertEqual(env["num_robots"], 10)

    def test_actors(self):
        actors = self.ir.institution.actors
        self.assertEqual(len(actors), 3)
        ids = [a.id for a in actors]
        self.assertIn("DISPATCHER", ids)
        self.assertIn("ROBOT", ids)
        self.assertIn("CUSTOMER", ids)

    def test_dispatcher_low_autonomy(self):
        dispatcher = [a for a in self.ir.institution.actors if a.id == "DISPATCHER"][0]
        self.assertEqual(dispatcher.autonomy, "low")
        self.assertIn("compute_routes_ecbs", dispatcher.capabilities)

    def test_robot_high_autonomy(self):
        robot = [a for a in self.ir.institution.actors if a.id == "ROBOT"][0]
        self.assertEqual(robot.autonomy, "high")

    def test_contracts(self):
        contracts = self.ir.institution.contracts
        self.assertEqual(len(contracts), 2)
        sla = [c for c in contracts if c.id == "DELIVERY_SLA"][0]
        self.assertIn("DISPATCHER", sla.parties)
        self.assertEqual(sla.governance.beta, 0.8)
        self.assertEqual(sla.governance.decision_holder, "DISPATCHER")

    def test_governance_alpha(self):
        sla = [c for c in self.ir.institution.contracts if c.id == "DELIVERY_SLA"][0]
        self.assertEqual(sla.governance.alpha, 0.9)

    def test_sharing_mode(self):
        sla = [c for c in self.ir.institution.contracts if c.id == "DELIVERY_SLA"][0]
        self.assertIsNotNone(sla.governance.sharing_mode)
        self.assertIn("->", sla.governance.sharing_mode)

    def test_protocols(self):
        protocols = self.ir.protocol.protocols
        self.assertEqual(len(protocols), 3)
        ids = [p.id for p in protocols]
        self.assertIn("TASK_DISPATCH", ids)
        self.assertIn("OBSTACLE_REPLAN", ids)
        self.assertIn("EMERGENCY_STOP", ids)

    def test_protocol_steps_flattened(self):
        task = [p for p in self.ir.protocol.protocols if p.id == "TASK_DISPATCH"][0]
        self.assertEqual(len(task.steps), 5)
        self.assertEqual(task.steps[0].type, "message")

    def test_events(self):
        self.assertTrue(len(self.ir.protocol.events) >= 3)

    def test_algorithms(self):
        algos = self.ir.algorithm.algorithms
        self.assertEqual(len(algos), 2)
        pathfinding = [a for a in algos if a.name == "pathfinding"][0]
        self.assertEqual(pathfinding.central, "ECBS")
        self.assertEqual(pathfinding.local, "tracking_only")

    def test_transitions(self):
        self.assertEqual(len(self.ir.transitions), 4)
        regimes = {t.from_regime for t in self.ir.transitions} | \
                  {t.to_regime for t in self.ir.transitions}
        self.assertIn("NORMAL", regimes)
        self.assertIn("CONGESTED", regimes)
        self.assertIn("EMERGENCY", regimes)

    def test_metrics(self):
        self.assertEqual(len(self.ir.metrics), 4)
        ids = [m.id for m in self.ir.metrics]
        self.assertIn("makespan", ids)


class TestLowerCSoS(unittest.TestCase):
    """Test lowering the C-SoS (taxi fleet) sample."""

    @classmethod
    def setUpClass(cls):
        cls.sos = parse_file(EXAMPLES / "c_sos_taxi_fleet.cadl")
        cls.ir = lower_to_ir(cls.sos)

    def test_name_and_type(self):
        self.assertEqual(self.ir.name, "AutonomousTaxiFleet")
        self.assertEqual(self.ir.sos_type, "Collaborative")

    def test_decentralized_governance(self):
        ride = [c for c in self.ir.institution.contracts if c.id == "RIDE_SERVICE"][0]
        self.assertEqual(ride.governance.beta, 0.1)
        self.assertEqual(ride.governance.decision_holder, "TAXI[*]")

    def test_local_planner(self):
        routing = [a for a in self.ir.algorithm.algorithms if a.name == "routing"][0]
        self.assertEqual(routing.local, "LRA_star")
        self.assertEqual(routing.central, "aggregation_only")

    def test_transitions(self):
        self.assertEqual(len(self.ir.transitions), 5)
        regimes = {t.from_regime for t in self.ir.transitions} | \
                  {t.to_regime for t in self.ir.transitions}
        self.assertIn("NORMAL", regimes)
        self.assertIn("PEAK_DEMAND", regimes)
        self.assertIn("INCIDENT_RESPONSE", regimes)


class TestValidation(unittest.TestCase):
    """Test IR validation."""

    def test_valid_ir(self):
        sos = parse_file(EXAMPLES / "a_sos_robot_delivery.cadl")
        ir = lower_to_ir(sos)
        errors = validate_ir(ir)
        self.assertEqual(errors, [])

    def test_valid_c_sos(self):
        sos = parse_file(EXAMPLES / "c_sos_taxi_fleet.cadl")
        ir = lower_to_ir(sos)
        errors = validate_ir(ir)
        self.assertEqual(errors, [])

    def test_no_actors(self):
        ir = SimIR(name="empty")
        errors = validate_ir(ir)
        self.assertTrue(any("No actors" in e for e in errors))

    def test_invalid_party_ref(self):
        ir = SimIR(
            name="test",
            institution=InstitutionLayer(
                actors=[ActorSpec(id="A", role="r", autonomy="high")],
                contracts=[ContractSpec(id="C1", parties=["A", "UNKNOWN"])],
            ),
        )
        errors = validate_ir(ir)
        self.assertTrue(any("UNKNOWN" in e for e in errors))

    def test_duplicate_actor_id(self):
        ir = SimIR(
            name="test",
            institution=InstitutionLayer(
                actors=[
                    ActorSpec(id="A", role="r", autonomy="high"),
                    ActorSpec(id="A", role="r2", autonomy="low"),
                ],
            ),
        )
        errors = validate_ir(ir)
        self.assertTrue(any("Duplicate actor" in e for e in errors))

    def test_governance_out_of_range(self):
        ir = SimIR(
            name="test",
            institution=InstitutionLayer(
                actors=[ActorSpec(id="A", role="r", autonomy="high")],
                contracts=[ContractSpec(
                    id="C1",
                    parties=["A"],
                    governance=GovernanceParams(beta=1.5),
                )],
            ),
        )
        errors = validate_ir(ir)
        self.assertTrue(any("beta" in e for e in errors))

    def test_unknown_protocol_in_transition(self):
        ir = SimIR(
            name="test",
            institution=InstitutionLayer(
                actors=[ActorSpec(id="A", role="r", autonomy="high")],
            ),
            transitions=[
                TransitionSpec(from_regime="X", to_regime="Y", protocol="NONEXISTENT"),
            ],
        )
        errors = validate_ir(ir)
        self.assertTrue(any("NONEXISTENT" in e for e in errors))

    def test_unknown_sender_in_protocol(self):
        ir = SimIR(
            name="test",
            institution=InstitutionLayer(
                actors=[ActorSpec(id="A", role="r", autonomy="high")],
            ),
            protocol=ProtocolLayer(
                protocols=[ProtocolSpec(
                    id="P1",
                    trigger="ev",
                    steps=[StepSpec(type="message", sender="GHOST", receiver="A", content="msg")],
                )],
            ),
        )
        errors = validate_ir(ir)
        self.assertTrue(any("GHOST" in e for e in errors))

    def test_valid_existing_examples(self):
        """All existing .cadl examples should validate as IR."""
        for cadl_file in EXAMPLES.glob("*.cadl"):
            sos = parse_file(cadl_file)
            ir = lower_to_ir(sos)
            errors = validate_ir(ir)
            self.assertEqual(errors, [], f"{cadl_file.name} failed: {errors}")


if __name__ == "__main__":
    unittest.main()
