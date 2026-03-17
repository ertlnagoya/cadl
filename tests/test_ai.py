"""Tests for CADL AI integration (NL-to-CADL generation)."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from cadl.ai.prompts import (
    build_system_prompt,
    build_user_prompt,
    build_retry_prompt,
    CADL_SYNTAX_REFERENCE,
    EXAMPLE_ROBOT_DELIVERY,
    EXAMPLE_HOUSEHOLD_CHORES,
)
from cadl.ai.nl_to_cadl import extract_cadl, generate_cadl, NlToCadlResult
from cadl.ai.llm_client import ClaudeClient, ClientConfig


# ---------------------------------------------------------------------------
# Prompt building tests
# ---------------------------------------------------------------------------

class TestPrompts:
    def test_system_prompt_contains_syntax_reference(self):
        prompt = build_system_prompt()
        assert "CADL Syntax Reference" in prompt
        assert "sos:" in prompt

    def test_system_prompt_contains_sos_types(self):
        prompt = build_system_prompt()
        assert "Directed" in prompt
        assert "Acknowledged" in prompt
        assert "Collaborative" in prompt
        assert "Virtual" in prompt

    def test_system_prompt_contains_examples(self):
        prompt = build_system_prompt()
        assert "RobotDeliverySystem" in prompt
        assert "FamilyHousehold" in prompt

    def test_system_prompt_contains_institutional_params(self):
        prompt = build_system_prompt()
        assert "alpha" in prompt
        assert "beta" in prompt
        assert "lambda" in prompt

    def test_system_prompt_contains_expression_syntax(self):
        prompt = build_system_prompt()
        assert "ACTOR[1..N]" in prompt
        assert "ACTOR[*]" in prompt
        assert "for all" in prompt

    def test_user_prompt_includes_description(self):
        desc = "A fleet of autonomous drones"
        prompt = build_user_prompt(desc)
        assert desc in prompt
        assert 'sos:' in prompt

    def test_retry_prompt_includes_errors(self):
        cadl = "sos:\n  name: Test"
        errors = ["Unknown actor: FOO", "beta out of range"]
        prompt = build_retry_prompt(cadl, errors)
        assert "Unknown actor: FOO" in prompt
        assert "beta out of range" in prompt
        assert cadl in prompt


# ---------------------------------------------------------------------------
# CADL extraction tests
# ---------------------------------------------------------------------------

class TestExtractCadl:
    def test_extract_plain_cadl(self):
        text = 'sos:\n  name: "Test"\n  type: Directed'
        result = extract_cadl(text)
        assert result.startswith("sos:")

    def test_extract_from_yaml_fenced(self):
        text = 'Here is the CADL:\n\n```yaml\nsos:\n  name: "Test"\n```\n\nDone.'
        result = extract_cadl(text)
        assert result.startswith("sos:")
        assert "```" not in result

    def test_extract_from_plain_fenced(self):
        text = '```\nsos:\n  name: "Test"\n```'
        result = extract_cadl(text)
        assert result.startswith("sos:")

    def test_extract_from_cadl_fenced(self):
        text = '```cadl\nsos:\n  name: "Test"\n```'
        result = extract_cadl(text)
        assert result.startswith("sos:")

    def test_extract_with_preamble(self):
        text = 'Here is the generated CADL definition:\n\nsos:\n  name: "Test"\n  type: Directed'
        result = extract_cadl(text)
        assert result.startswith("sos:")

    def test_extract_empty_returns_stripped(self):
        result = extract_cadl("  some text  ")
        assert result == "some text"


# ---------------------------------------------------------------------------
# Client config tests
# ---------------------------------------------------------------------------

class TestClientConfig:
    def test_default_config(self):
        config = ClientConfig()
        assert config.model == "claude-sonnet-4-20250514"
        assert config.max_tokens == 4096
        assert config.temperature == 0.0

    def test_custom_config(self):
        config = ClientConfig(model="claude-opus-4-20250514", max_tokens=8192)
        assert config.model == "claude-opus-4-20250514"
        assert config.max_tokens == 8192


# ---------------------------------------------------------------------------
# Client import error test
# ---------------------------------------------------------------------------

class TestClaudeClient:
    def test_missing_anthropic_raises_import_error(self):
        client = ClaudeClient()
        with patch.dict("sys.modules", {"anthropic": None}):
            with pytest.raises(ImportError, match="anthropic"):
                client._client = None  # Reset
                client._get_client()

    def test_missing_api_key_raises_value_error(self):
        client = ClaudeClient(ClientConfig(api_key=None))
        # Mock anthropic import to succeed but no key
        mock_anthropic = MagicMock()
        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            with patch.dict("os.environ", {}, clear=True):
                import os
                orig_get = os.environ.get
                with patch.object(os.environ, "get", return_value=None):
                    client._client = None
                    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
                        client._get_client()


# ---------------------------------------------------------------------------
# Mock-based pipeline tests
# ---------------------------------------------------------------------------

# A valid CADL output that the mock LLM would return
MOCK_CADL_RESPONSE = """\
sos:
  name: "DroneSpraySystem"
  type: Acknowledged
  version: "1.0.0"

  actors:
    - id: CONTROLLER
      role: "ground_controller"
      autonomy: low
      capabilities:
        - plan_routes
        - monitor_fleet

    - id: "DRONE[1..N]"
      role: "spray_drone"
      autonomy: high
      capabilities:
        - autonomous_flight
        - spray_control

  contracts:
    - id: SPRAY_COVERAGE
      parties:
        - CONTROLLER
        - "DRONE[*]"
      assume:
        - "CONTROLLER.is_operational == true"
      guarantee:
        - "coverage_area >= target_area * 0.95"
      authority:
        decision_holder: CONTROLLER
        beta: 0.3
      information:
        alpha: 0.7
      incentives:
        type: reputation
        lambda: 0.4
        rules:
          - "reward(DRONE[i], 10) when area_completed"
      duration: indefinite

  protocols:
    - id: EMERGENCY_RETURN
      trigger: "low_battery_detected_by(DRONE[i])"
      steps:
        - "DRONE[i] -> CONTROLLER : low_battery_alert"
        - "CONTROLLER : reassign_area"
        - "CONTROLLER -> DRONE[*] : updated_assignments"
      timing:
        max_total: 30s

  metrics:
    - id: spray_coverage_rate
      formula: "sprayed_area / total_target_area"
      target: ">= 0.95"
"""


class TestGenerateCadlPipeline:
    def _make_mock_client(self, response: str) -> ClaudeClient:
        """Create a mock ClaudeClient that returns the given response."""
        client = ClaudeClient()
        client.generate = MagicMock(return_value=response)
        client.generate_with_history = MagicMock(return_value=response)
        return client

    def test_successful_generation(self):
        client = self._make_mock_client(MOCK_CADL_RESPONSE)
        result = generate_cadl("A drone spray system", client=client)

        assert result.cadl_source.startswith("sos:")
        assert result.sos is not None
        assert result.sos.name == "DroneSpraySystem"
        assert len(result.sos.actors) == 2
        assert len(result.sos.contracts) == 1
        assert len(result.sos.protocols) == 1
        assert not result.errors
        assert not result.retried

    def test_generation_with_markdown_fences(self):
        fenced = f"Here is the CADL:\n\n```yaml\n{MOCK_CADL_RESPONSE}\n```\n"
        client = self._make_mock_client(fenced)
        result = generate_cadl("drone system", client=client)

        assert result.sos is not None
        assert result.sos.name == "DroneSpraySystem"
        assert not result.errors

    def test_retry_on_parse_error(self):
        """First call returns invalid CADL, retry returns valid."""
        client = ClaudeClient()
        call_count = [0]

        def mock_generate(system_prompt, user_message):
            call_count[0] += 1
            return "this is not valid CADL"

        def mock_generate_with_history(system_prompt, messages):
            return MOCK_CADL_RESPONSE

        client.generate = mock_generate
        client.generate_with_history = mock_generate_with_history

        result = generate_cadl("drone system", client=client)

        assert result.sos is not None
        assert result.sos.name == "DroneSpraySystem"
        assert result.retried

    def test_all_retries_fail(self):
        """Both initial and retry return invalid CADL."""
        client = ClaudeClient()
        client.generate = MagicMock(return_value="invalid yaml: [[[")
        client.generate_with_history = MagicMock(return_value="still invalid: [[[")

        result = generate_cadl("test", client=client, max_retries=1)

        assert result.errors
        assert result.sos is None

    def test_type_check_errors_trigger_retry(self):
        """CADL parses but has type errors, triggers retry."""
        # A CADL that parses but has a type error (reference to undefined actor)
        bad_cadl = """\
sos:
  name: "BadSystem"
  type: Directed
  version: "1.0.0"

  actors:
    - id: MANAGER
      role: "manager"
      autonomy: low

  contracts:
    - id: BAD_CONTRACT
      parties:
        - MANAGER
        - UNDEFINED_ACTOR
      assume:
        - "true"
      guarantee:
        - "true"
      authority:
        decision_holder: MANAGER
        beta: 0.5
      information:
        alpha: 0.5
      duration: indefinite
"""
        client = ClaudeClient()
        client.generate = MagicMock(return_value=bad_cadl)
        client.generate_with_history = MagicMock(return_value=MOCK_CADL_RESPONSE)

        result = generate_cadl("test", client=client)

        # Should have retried and succeeded
        assert result.sos is not None
        assert result.retried

    def test_no_retry_when_max_retries_zero(self):
        client = self._make_mock_client("invalid")
        result = generate_cadl("test", client=client, max_retries=0)
        assert result.errors
        client.generate_with_history.assert_not_called()


# ---------------------------------------------------------------------------
# NlToCadlResult tests
# ---------------------------------------------------------------------------

class TestNlToCadlResult:
    def test_default_values(self):
        result = NlToCadlResult(cadl_source="sos:")
        assert result.sos is None
        assert result.validation is None
        assert result.errors == []
        assert result.retried is False


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------

class TestCLIAi:
    def test_ai_command_no_description(self):
        from cadl.cli import main
        ret = main(["ai"])
        assert ret == 1

    def test_ai_command_missing_input_file(self):
        from cadl.cli import main
        ret = main(["ai", "-f", "/nonexistent/file.txt"])
        assert ret == 1
