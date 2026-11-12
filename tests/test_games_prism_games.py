"""The export of I.11 run through PRISM-games (tests/prism_games_check.py).

The tests are skipped when PRISM-games is not installed: set
CADL_PRISM_GAMES to the `prism` script of a PRISM-games distribution, or
put a `prism-games` on PATH. CADL_PRISM_GAMES_FUZZ sets the number of
models of tests/games_fuzz.py that are compared (default 8), and
CADL_REQUIRE_PRISM_GAMES=1 turns a missing PRISM-games into an error
instead of a skip (as in the CI)."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent))

from prism_games_check import (  # noqa: E402
    EXAMPLES,
    Games,
    check_fuzz,
    compare_model,
    find_prism_games,
    run_prism,
)

from cadl.parser import parse, parse_file  # noqa: E402
from cadl.stochastic import analyse, export_prism  # noqa: E402

PRISM_GAMES = find_prism_games()
if PRISM_GAMES is None and os.environ.get("CADL_REQUIRE_PRISM_GAMES"):
    raise RuntimeError("CADL_REQUIRE_PRISM_GAMES is set, and PRISM-games is not found "
                       "(CADL_PRISM_GAMES or prism-games on PATH)")
needs_prism_games = pytest.mark.skipif(
    PRISM_GAMES is None,
    reason="PRISM-games is not installed (CADL_PRISM_GAMES or prism-games on PATH)")


def _model(path: Path):
    analysis = analyse(parse_file(str(path)))
    assert analysis.ok
    return analysis.model


@needs_prism_games
class TestPrismGames:
    def test_version(self):
        out = run_prism(PRISM_GAMES, "smg\n\nplayer a\nendplayer\n\nmodule m\n"
                        "  x : bool init false;\n  [] true -> true;\nendmodule\n", [])
        assert re.search(r"^Version: 3\.", out, re.M), out[:400]

    @pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.stem)
    def test_examples(self, path):
        stats: dict = {}
        assert compare_model(_model(path), PRISM_GAMES, stats) == []
        assert stats["designs"] >= 2 and stats["values"] > 0

    def test_fuzz_models(self):
        count = int(os.environ.get("CADL_PRISM_GAMES_FUZZ", "8"))
        stats: dict = {}
        assert check_fuzz(PRISM_GAMES, 11, count, stats) == []
        assert stats["fuzz models"] == count

    def test_player_without_a_command(self):
        # Every command unowned: the exported file has only players without
        # actions. Up to 0.3.26 it had no player, and PRISM-games refused it.
        robot = next(p for p in EXAMPLES if p.stem == "games_robot_delivery")
        data = yaml.safe_load(robot.read_text(encoding="utf-8"))
        model = data["sos"]["model"]
        for c in model["commands"]:
            c.pop("owner", None)
            if c["action"] == "careless":
                c["guard"] = "false"
        model["profiles"] = {name: [] for name in model["profiles"]}
        parsed = analyse(parse(yaml.safe_dump(data, sort_keys=False)))
        assert parsed.ok
        for text in export_prism(parsed.model).values():
            assert "\nplayer R_\nendplayer\n" in text
            out = run_prism(PRISM_GAMES, text, ['<<R_>> R{"effort"}max=? [ F "delivered" ]'])
            assert re.search(r"^Result: ", out, re.M)

    @pytest.mark.parametrize("name", ["loc", "goal", "a", "p", "steps", "main"])
    def test_player_named_like_another_identifier(self, name):
        # I.11 leaves open whether PRISM-games accepts such a player;
        # PRISM-games 3.2.4 does.
        text = f"""
sos:
  name: "N"
  type: Acknowledged
  extensions: [ {{ stochastic: 0.1 }}, {{ games: 0.1 }} ]
  actors: [ {{ id: ACT, role: "x" }} ]
  model:
    kind: smg
    parameters: [ {{ id: p, range: [0, 1], value: 0.5 }} ]
    variables: [ {{ id: loc, type: "{{S0, S1, Goal}}", init: S0 }} ]
    labels: {{ goal: "loc == Goal" }}
    players: [ {{ id: {name}, actors: [ACT] }} ]
    commands:
      - {{ action: a, owner: {name}, guard: "loc == S0",
          updates: [ {{ prob: "p", set: {{ loc: S1 }} }}, {{ prob: rest, set: {{ loc: Goal }} }} ] }}
      - {{ action: b, owner: {name}, guard: "loc == S0", updates: [ {{ set: {{ loc: Goal }} }} ] }}
      - {{ action: c, guard: "loc == S1", updates: [ {{ set: {{ loc: Goal }} }} ] }}
    rewards: [ {{ id: steps, items: [ {{ value: 1 }} ] }} ]
    profiles: {{ sigma: [ {{ choose: b }} ] }}
    compliance: sigma
    payoffs: [ {{ player: {name}, until: "goal", terms: [ {{ reward: steps }} ] }} ]
    equilibria: [ {{ id: m }} ]
"""
        model = analyse(parse(text)).model
        stats: dict = {}
        assert compare_model(model, PRISM_GAMES, stats) == []
        assert stats["verdicts"] == 1
        with Games():
            exported = export_prism(model)["N_base.prism"]
        out = run_prism(PRISM_GAMES, exported, [f'<<{name}>> R{{"steps"}}max=? [ F "goal" ]'])
        assert re.search(r"^Result: 1\.5", out, re.M)
