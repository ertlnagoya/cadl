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
