"""Tests for rendering expressions back to CADL source text."""

import pytest

from cadl.ast_nodes import StringLiteral
from cadl.parser import parse_expr
from cadl.unparse import expr_to_source


@pytest.mark.parametrize("text", [
    "a + b * c",
    "(a + b) * c",
    "a - (b - c)",
    "a / b / c",
    "active_tasks > capacity * 0.8",
    "a AND b OR c",
    "a AND (b OR c)",
    "NOT a AND b",
    "NOT (a AND b)",
    "flag == false AND level <= 0.4",
    "wait(agentMotivation[i] * maxClaimDelaySec)",
    "count(a) / count(b)",
    "ROBOT[*]",
    "DISPATCHER.is_operational == true",
    "response_time <= 200ms",
    'mode == "auto"',
    "for all r in ROBOT[*]: r.status != Collision",
    "x > 30s AND NOT (for all r in ROBOT[*]: r.status != Collision)",
])
def test_round_trip(text):
    assert expr_to_source(parse_expr(text)) == text
    # and the rendered text parses back to the same tree
    assert parse_expr(expr_to_source(parse_expr(text))) == parse_expr(text)


def test_top_level_string_is_free_text():
    assert expr_to_source(StringLiteral("robots stay within the zone")) == \
        "robots stay within the zone"
