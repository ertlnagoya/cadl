"""Generated code distinguishes declared actors from state variables."""

from cadl.codegen.expr_compiler import CompilerContext, declared_actors, expr_to_python
from cadl.codegen.solidity.solidity_expr import (
    SolidityContext,
    expr_to_solidity,
    predicate_to_solidity,
)
from cadl.parser import parse_expr


def _py(text, actors=("ROBOT", "DISPATCHER")):
    with declared_actors(actors):
        return expr_to_python(parse_expr(text), CompilerContext())


def _sol(text, actors=("ROBOT", "DISPATCHER")):
    with declared_actors(actors):
        return predicate_to_solidity(parse_expr(text), SolidityContext())


class TestPython:

    def test_state_variable(self):
        assert _py("delivery_time <= promised_time * 1.2") == (
            "(ctx.state['delivery_time'] <= (ctx.state['promised_time'] * 1.2))"
        )

    def test_declared_actor_member(self):
        assert _py("DISPATCHER.is_operational == true") == (
            "(ctx.actors['DISPATCHER'].state['is_operational'] == True)"
        )

    def test_indexed_actor_member(self):
        assert _py("ROBOT[i].battery > 20") == (
            "(ctx.actors['ROBOT'][i].state['battery'] > 20)"
        )

    def test_quantifier_variable_is_the_element(self):
        assert _py("for all r in ROBOT[*]: r.status != Collision") == (
            "all((r.state['status'] != ctx.state['Collision']) for r in ctx.actors['ROBOT'])"
        )

    def test_comprehension_over_range(self):
        assert _py("sum(ROBOT[i].goal_count for i in 1..5)") == (
            "ctx.sum(ctx.actors['ROBOT'][i].state['goal_count'] for i in range(1, 5 + 1))"
        )

    def test_comprehension_symbolic_end(self):
        assert _py("sum(ROBOT[i].goal_count for i in 1..N)") == (
            "ctx.sum(ctx.actors['ROBOT'][i].state['goal_count'] "
            "for i in range(1, ctx.state['N'] + 1))"
        )

    def test_unknown_actor_set_keeps_actor_lookup(self):
        assert expr_to_python(parse_expr("x > 1"), CompilerContext()) == (
            "(ctx.actors['x'] > 1)"
        )

    def test_scope_ends_with_the_block(self):
        with declared_actors(["A"]):
            pass
        assert CompilerContext().actor_names is None


class TestSolidity:

    def test_numeric_state_variable(self):
        assert _sol("active_tasks > 3") == '(stateUint["active_tasks"] > 3)'

    def test_boolean_state_variable(self):
        assert _sol("emergency_active == false") == '(stateBool["emergency_active"] == false)'
        assert _sol("ready AND NOT blocked") == '(stateBool["ready"] && (!stateBool["blocked"]))'
        assert _sol("ready") == 'stateBool["ready"]'

    def test_declared_actor_is_not_state(self):
        assert _sol("DISPATCHER.is_operational == true") == (
            "(actors_dispatcher.is_operational == true)"
        )

    def test_index_is_rendered_as_a_name(self):
        with declared_actors(["ROBOT"]):
            assert expr_to_solidity(parse_expr("ROBOT[i]"), SolidityContext()) == "actors_robot[i]"
