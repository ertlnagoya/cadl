"""Keywords end at a word boundary: a longer name is never split."""

import pytest

from cadl.ast_nodes import ActorRef, BinaryOp, BoolLiteral, MemberAccess, UnaryOp
from cadl.parser import parse, parse_expr
from cadl.type_checker import type_check


@pytest.mark.parametrize("name", [
    "NOT_READY", "NOTIFIED", "ORDER_COUNT", "ANDROID", "INDEX", "inventory",
    "information_ok", "existsx", "forecast", "format", "trueish", "false_alarm",
])
def test_name_starting_with_a_keyword_is_one_identifier(name):
    assert parse_expr(name) == ActorRef(name=name)


def test_comparison_with_such_a_name():
    assert parse_expr("NOTIFIED == true") == BinaryOp(
        "==", ActorRef("NOTIFIED"), BoolLiteral(True)
    )


def test_member_access_on_such_an_actor():
    assert parse_expr("NOTIFIER.ready") == MemberAccess(ActorRef("NOTIFIER"), "ready")


@pytest.mark.parametrize("text", ["a ORb", "a ANDb", "for all x inventory: p(x)"])
def test_keyword_glued_to_a_name_is_not_an_operator(text):
    with pytest.raises(Exception):
        parse_expr(text)


def test_keywords_still_work():
    assert parse_expr("NOT a AND b") == BinaryOp(
        "AND", UnaryOp("NOT", ActorRef("a")), ActorRef("b")
    )
    assert parse_expr("NOT(a)") == UnaryOp("NOT", ActorRef("a"))
    assert parse_expr("for  all x in X: p(x)").quantifier == "for_all"
    assert parse_expr("exists x in X: p(x)").quantifier == "exists"


def test_declared_actor_named_like_a_keyword_passes_check():
    source = (
        "sos:\n"
        '  name: "T"\n'
        "  type: Directed\n"
        "  actors:\n"
        "    - id: NOTIFIER\n"
        "      role: r\n"
        "  contracts:\n"
        "    - id: C1\n"
        "      parties: [NOTIFIER]\n"
        "      assume:\n"
        '        - "NOTIFIER.ready == true"\n'
    )
    assert type_check(parse(source)).errors == []
