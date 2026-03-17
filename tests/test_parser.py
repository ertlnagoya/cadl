"""Tests for the CADL parser."""

import pytest

from cadl.parser import parse, CADLParseError
from cadl.ast_nodes import (
    SoSDefinition,
    SoSType,
    AutonomyLevel,
    StringLiteral,
)


class TestMinimalParse:
    """Test parsing of minimal CADL documents."""

    def test_minimal_sos(self):
        source = '''\
sos:
  name: "TestSoS"
'''
        result = parse(source)
        assert isinstance(result, SoSDefinition)
        assert result.name == "TestSoS"

    def test_sos_with_type(self):
        source = '''\
sos:
  name: "TestSoS"
  type: Collaborative
'''
        result = parse(source)
        assert result.type == SoSType.COLLABORATIVE

    def test_sos_with_version(self):
        source = '''\
sos:
  name: "TestSoS"
  version: 1.0.0
'''
        result = parse(source)
        assert result.version == "1.0.0"


class TestActorParsing:
    """Test parsing of actor definitions."""

    def test_single_actor(self):
        source = '''\
sos:
  name: "TestSoS"
  actors:
    - id: DISPATCHER
      role: "planner"
      autonomy: low
'''
        result = parse(source)
        assert len(result.actors) == 1
        actor = result.actors[0]
        assert actor.id.name == "DISPATCHER"
        assert actor.role == "planner"
        assert actor.autonomy == AutonomyLevel.LOW

    def test_actor_with_range(self):
        source = '''\
sos:
  name: "TestSoS"
  actors:
    - id: ROBOT[1..N]
      role: "vehicle"
      autonomy: high
'''
        result = parse(source)
        assert len(result.actors) == 1
        actor = result.actors[0]
        assert actor.id.name == "ROBOT"
        assert actor.id.index is not None

    def test_multiple_actors(self):
        source = '''\
sos:
  name: "TestSoS"
  actors:
    - id: DISPATCHER
      role: "planner"
      autonomy: low
    - id: ROBOT[1..5]
      role: "vehicle"
      autonomy: high
'''
        result = parse(source)
        assert len(result.actors) == 2


class TestContractParsing:
    """Test parsing of contract definitions."""

    def test_basic_contract(self):
        source = '''\
sos:
  name: "TestSoS"
  actors:
    - id: A
      role: "role_a"
      autonomy: low
    - id: B
      role: "role_b"
      autonomy: high
  contracts:
    - id: CONTRACT1
      parties: [A, B]
      authority:
        beta: 0.5
      duration: indefinite
'''
        result = parse(source)
        assert len(result.contracts) == 1
        contract = result.contracts[0]
        assert contract.id == "CONTRACT1"
        assert len(contract.parties) == 2
        assert contract.authority.beta == 0.5
        assert contract.duration == "indefinite"


class TestProtocolParsing:
    """Test parsing of protocol definitions."""

    def test_basic_protocol(self):
        source = '''\
sos:
  name: "TestSoS"
  actors:
    - id: A
      role: "sender"
      autonomy: low
    - id: B
      role: "receiver"
      autonomy: low
  protocols:
    - id: SIMPLE_PROTO
      trigger: "event_occurred()"
      steps:
        - A -> B : notify
        - B : process_notification
      timing:
        max_total: 100ms
'''
        result = parse(source)
        assert len(result.protocols) == 1
        proto = result.protocols[0]
        assert proto.id == "SIMPLE_PROTO"
        assert len(proto.steps) == 2


class TestMetricParsing:
    """Test parsing of metric definitions."""

    def test_metrics(self):
        source = '''\
sos:
  name: "TestSoS"
  metrics:
    - id: success_rate
      formula: "count(success) / count(total)"
      target: ">= 0.95"
'''
        result = parse(source)
        assert len(result.metrics) == 1
        assert result.metrics[0].id == "success_rate"


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_empty_contracts_list(self):
        source = '''\
sos:
  name: "TestSoS"
  contracts: []
'''
        result = parse(source)
        assert result.contracts == []

    def test_empty_protocols_list(self):
        source = '''\
sos:
  name: "TestSoS"
  protocols: []
'''
        result = parse(source)
        assert result.protocols == []

    def test_invalid_sos_type(self):
        source = '''\
sos:
  name: "TestSoS"
  type: InvalidType
'''
        with pytest.raises(CADLParseError, match="Invalid SoS type"):
            parse(source)

    def test_yaml_syntax_error(self):
        source = "sos:\n  name: [[[invalid"
        with pytest.raises(CADLParseError, match="YAML"):
            parse(source)

    def test_unicode_description(self):
        source = '''\
sos:
  name: "テストSoS"
  description: "日本語の説明文。ロボット配送システム。"
  type: Directed
'''
        result = parse(source)
        assert result.name == "テストSoS"
        assert "日本語" in result.description

    def test_missing_sos_key(self):
        source = "something_else:\n  key: value"
        with pytest.raises(CADLParseError, match="sos"):
            parse(source)


class TestContextBlock:
    """Test context/environment section parsing."""

    def test_context_with_environment(self):
        source = '''\
sos:
  name: "TestSoS"
  context:
    environment:
      num_sensors: 50
      update_interval: "1s"
    assumptions:
      - "network is available"
      - "sensors are calibrated"
'''
        result = parse(source)
        assert result.context is not None
        assert result.context.environment is not None
        assert len(result.context.assumptions) == 2


class TestTransitionParsing:
    """Test transition section parsing."""

    def test_transitions(self):
        source = '''\
sos:
  name: "TestSoS"
  actors:
    - id: A
      role: "r"
      autonomy: low
  transitions:
    - from: normal
      to: degraded
      condition: "load > 0.9"
      safety_invariant: "system_stable == true"
    - from: degraded
      to: normal
      condition: "load < 0.5"
'''
        result = parse(source)
        assert len(result.transitions) == 2
        assert result.transitions[0].from_regime == "normal"
        assert result.transitions[0].to_regime == "degraded"
        assert result.transitions[0].condition == "load > 0.9"
        assert result.transitions[0].safety_invariant == "system_stable == true"


class TestAlgorithmParsing:
    """Test algorithm section parsing."""

    def test_algorithms(self):
        source = '''\
sos:
  name: "TestSoS"
  algorithms:
    routing:
      central: "dijkstra"
      local: "a_star"
    scheduling:
      central: "round_robin"
'''
        result = parse(source)
        assert len(result.algorithms) == 2
        names = {a.name for a in result.algorithms}
        assert "routing" in names
        assert "scheduling" in names
        routing = next(a for a in result.algorithms if a.name == "routing")
        assert routing.central == "dijkstra"
        assert routing.local == "a_star"


class TestComments:
    """Test that comments are properly handled."""

    def test_inline_comments(self):
        source = '''\
sos:
  name: "TestSoS"  # This is a comment
  type: Collaborative  # Another comment
'''
        result = parse(source)
        assert result.name == "TestSoS"
        assert result.type == SoSType.COLLABORATIVE


class TestVerificationBlock:
    """Test parsing of verification blocks."""

    def test_verification_block(self):
        source = '''\
sos:
  name: "TestSoS"
  type: Directed
  version: "1.0.0"

  actors:
    - id: AGENT
      role: "worker"
      autonomy: low

  verification:
    - id: CHECK_CONSISTENCY
      type: consistency
      target: DELIVERY_SLA
    - id: CHECK_DEADLOCK
      type: deadlock
      property: "no_circular_wait"
'''
        result = parse(source)
        assert len(result.verifications) == 2
        assert result.verifications[0].id == "CHECK_CONSISTENCY"
        assert result.verifications[0].type == "consistency"
        assert result.verifications[0].target == "DELIVERY_SLA"
        assert result.verifications[1].id == "CHECK_DEADLOCK"
        assert result.verifications[1].type == "deadlock"
        assert result.verifications[1].property == "no_circular_wait"

    def test_empty_verification_block(self):
        source = '''\
sos:
  name: "TestSoS"
  verification: []
'''
        result = parse(source)
        assert result.verifications == []


class TestCodegenBlock:
    """Test parsing of codegen blocks."""

    def test_codegen_block(self):
        source = '''\
sos:
  name: "TestSoS"
  type: Directed
  version: "1.0.0"

  actors:
    - id: AGENT
      role: "worker"
      autonomy: low

  codegen:
    - target: python
      output: "./generated"
      mappings:
        AGENT: "agent_module.AgentImpl"
    - target: solidity
      output: "./contracts"
'''
        result = parse(source)
        assert len(result.codegen) == 2
        assert result.codegen[0].target == "python"
        assert result.codegen[0].output == "./generated"
        assert result.codegen[0].mappings == {"AGENT": "agent_module.AgentImpl"}
        assert result.codegen[1].target == "solidity"
        assert result.codegen[1].output == "./contracts"
        assert result.codegen[1].mappings == {}

    def test_empty_codegen_block(self):
        source = '''\
sos:
  name: "TestSoS"
  codegen: []
'''
        result = parse(source)
        assert result.codegen == []
