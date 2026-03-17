"""Tests for the CADL type checker."""

import pytest

from cadl.parser import parse
from cadl.type_checker import type_check


class TestActorReferenceCheck:
    """Test that undefined actor references are caught."""

    def test_valid_actor_refs(self):
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
    - id: C1
      parties: [A, B]
      authority:
        decision_holder: A
        beta: 0.5
      duration: indefinite
'''
        sos = parse(source)
        result = type_check(sos)
        assert result.ok

    def test_undefined_actor_in_contract(self):
        source = '''\
sos:
  name: "TestSoS"
  actors:
    - id: A
      role: "role_a"
      autonomy: low
  contracts:
    - id: C1
      parties: [A, UNKNOWN]
      authority:
        beta: 0.5
      duration: indefinite
'''
        sos = parse(source)
        result = type_check(sos)
        assert not result.ok
        assert any("UNKNOWN" in e.message for e in result.errors)

    def test_undefined_actor_in_protocol(self):
        source = '''\
sos:
  name: "TestSoS"
  actors:
    - id: A
      role: "role_a"
      autonomy: low
  protocols:
    - id: P1
      trigger: "event()"
      steps:
        - A -> MISSING : msg
'''
        sos = parse(source)
        result = type_check(sos)
        assert not result.ok
        assert any("MISSING" in e.message for e in result.errors)


class TestParameterRanges:
    """Test that parameter range constraints are enforced."""

    def test_valid_beta(self):
        source = '''\
sos:
  name: "TestSoS"
  actors:
    - id: A
      role: "r"
      autonomy: low
  contracts:
    - id: C1
      parties: [A]
      authority:
        beta: 0.5
      duration: indefinite
'''
        sos = parse(source)
        result = type_check(sos)
        assert result.ok

    def test_invalid_beta(self):
        source = '''\
sos:
  name: "TestSoS"
  actors:
    - id: A
      role: "r"
      autonomy: low
  contracts:
    - id: C1
      parties: [A]
      authority:
        beta: 1.5
      duration: indefinite
'''
        sos = parse(source)
        result = type_check(sos)
        assert not result.ok
        assert any("beta" in e.message for e in result.errors)

    def test_invalid_alpha(self):
        source = '''\
sos:
  name: "TestSoS"
  actors:
    - id: A
      role: "r"
      autonomy: low
  contracts:
    - id: C1
      parties: [A]
      information:
        alpha: -0.1
      duration: indefinite
'''
        sos = parse(source)
        result = type_check(sos)
        assert not result.ok
        assert any("alpha" in e.message for e in result.errors)

    def test_invalid_lambda(self):
        source = '''\
sos:
  name: "TestSoS"
  actors:
    - id: A
      role: "r"
      autonomy: low
  contracts:
    - id: C1
      parties: [A]
      incentives:
        lambda: 2.0
      duration: indefinite
'''
        sos = parse(source)
        result = type_check(sos)
        assert not result.ok
        assert any("lambda" in e.message for e in result.errors)


class TestDuplicateDetection:
    """Test duplicate ID detection."""

    def test_duplicate_contract_id(self):
        source = '''\
sos:
  name: "TestSoS"
  actors:
    - id: A
      role: "r"
      autonomy: low
  contracts:
    - id: SAME_ID
      parties: [A]
      duration: indefinite
    - id: SAME_ID
      parties: [A]
      duration: indefinite
'''
        sos = parse(source)
        result = type_check(sos)
        assert not result.ok
        assert any("Duplicate contract" in e.message for e in result.errors)

    def test_duplicate_actor_id(self):
        source = '''\
sos:
  name: "TestSoS"
  actors:
    - id: A
      role: "r1"
      autonomy: low
    - id: A
      role: "r2"
      autonomy: high
'''
        sos = parse(source)
        result = type_check(sos)
        assert not result.ok
        assert any("Duplicate actor" in e.message for e in result.errors)


class TestAlgorithmCheck:
    """Test algorithm duplicate name detection."""

    def test_duplicate_algorithm_name_warning(self):
        source = '''\
sos:
  name: "TestSoS"
  actors:
    - id: A
      role: "r"
      autonomy: low
  algorithms:
    routing:
      central: "dijkstra"
    routing:
      central: "a_star"
'''
        sos = parse(source)
        # YAML merges duplicate keys, so this tests the checker's own logic
        # We need to manually add a duplicate for the check
        from cadl.ast_nodes import AlgorithmDef
        sos.algorithms.append(AlgorithmDef(name="routing", central="a_star"))
        result = type_check(sos)
        assert any("Duplicate algorithm" in w.message for w in result.warnings)


class TestMetricFormulaValidation:
    """Test metric formula parsing validation."""

    def test_valid_formula(self):
        source = '''\
sos:
  name: "TestSoS"
  actors:
    - id: A
      role: "r"
      autonomy: low
  metrics:
    - id: latency
      formula: "mean(response_times)"
      target: "<= 100ms"
'''
        sos = parse(source)
        result = type_check(sos)
        # Valid formula should not produce warnings
        assert not any("formula is not a valid" in w.message for w in result.warnings)

    def test_duplicate_metric_id(self):
        source = '''\
sos:
  name: "TestSoS"
  actors:
    - id: A
      role: "r"
      autonomy: low
  metrics:
    - id: latency
      formula: "x"
      target: "<= 100"
    - id: latency
      formula: "y"
      target: "<= 200"
'''
        sos = parse(source)
        result = type_check(sos)
        assert not result.ok
        assert any("Duplicate metric" in e.message for e in result.errors)


class TestBoundaryValues:
    """Test boundary values for institutional parameters."""

    def test_alpha_zero(self):
        source = '''\
sos:
  name: "TestSoS"
  actors:
    - id: A
      role: "r"
      autonomy: low
    - id: B
      role: "r"
      autonomy: low
  contracts:
    - id: C1
      parties:
        - A
        - B
      assume:
        - "true"
      guarantee:
        - "true"
      information:
        alpha: 0.0
      duration: indefinite
'''
        sos = parse(source)
        result = type_check(sos)
        assert result.ok

    def test_alpha_one(self):
        source = '''\
sos:
  name: "TestSoS"
  actors:
    - id: A
      role: "r"
      autonomy: low
    - id: B
      role: "r"
      autonomy: low
  contracts:
    - id: C1
      parties:
        - A
        - B
      assume:
        - "true"
      guarantee:
        - "true"
      information:
        alpha: 1.0
      duration: indefinite
'''
        sos = parse(source)
        result = type_check(sos)
        assert result.ok


class TestTransitionProtocolRef:
    """Test transition undefined protocol reference."""

    def test_undefined_protocol_warning(self):
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
      protocol: NONEXISTENT_PROTOCOL
'''
        sos = parse(source)
        result = type_check(sos)
        assert any("undefined protocol" in w.message.lower() for w in result.warnings)
