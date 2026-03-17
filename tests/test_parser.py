"""Tests for the CADL parser."""

import pytest

from cadl.parser import parse
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
