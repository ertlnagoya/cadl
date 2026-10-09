"""`cadl check`: which names in a predicate must be declared actors."""

from cadl.parser import parse
from cadl.type_checker import type_check


def _errors(assume=None, guarantee=None):
    def block(key, items):
        if not items:
            return ""
        return f"      {key}:\n" + "".join(f'        - "{i}"\n' for i in items)

    source = (
        "sos:\n"
        '  name: "T"\n'
        "  type: Directed\n"
        "  actors:\n"
        "    - id: DISPATCHER\n"
        "      role: coordinator\n"
        "    - id: ROBOT[1..N]\n"
        "      role: agent\n"
        "  contracts:\n"
        "    - id: C1\n"
        '      parties: [DISPATCHER, "ROBOT[*]"]\n'
        + block("assume", assume) + block("guarantee", guarantee)
    )
    return [str(e) for e in type_check(parse(source)).errors]


def test_bare_name_is_a_state_variable():
    assert _errors(assume=["system_ready"]) == []
    assert _errors(guarantee=["delivery_time <= 300s"]) == []


def test_declared_actors_are_accepted():
    assert _errors(assume=["DISPATCHER.is_operational == true", "ROBOT[i].battery > 20"]) == []


def test_member_access_on_undeclared_actor_alone():
    (err,) = _errors(assume=["GHOST.is_up"])
    assert "Undefined actor 'GHOST'" in err


def test_undeclared_actor_inside_comparison():
    (err,) = _errors(guarantee=["GHOST.x > 5"])
    assert "Undefined actor 'GHOST'" in err


def test_undeclared_actor_inside_logic():
    errs = _errors(assume=["ok AND NOT (GHOST.x > 5 OR PHANTOM[i].y == 1)"])
    assert len(errs) == 2
    assert any("'GHOST'" in e for e in errs) and any("'PHANTOM'" in e for e in errs)


def test_quantifier_variable_is_not_an_actor():
    assert _errors(assume=["for all r in ROBOT[*]: r.battery > 20"]) == []


def test_quantifier_domain_is_checked():
    (err,) = _errors(assume=["for all g in GHOST[*]: g.battery > 20"])
    assert "Undefined actor 'GHOST'" in err


def test_comprehension_variable_and_domain():
    assert _errors(guarantee=["sum(r.load for r in ROBOT[*]) < 10"]) == []
    assert _errors(guarantee=["sum(ROBOT[i].load for i in 1..5) < 10"]) == []
    (err,) = _errors(guarantee=["sum(g.load for g in GHOST[*]) < 10"])
    assert "Undefined actor 'GHOST'" in err
