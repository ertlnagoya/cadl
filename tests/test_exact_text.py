"""Exact numbers of any size in the results of game-check and model-check.

From Python 3.11 ``str`` refuses integers of more than 4300 digits. Exact
results of a long model (I.13, H.8) can be longer, and were a crash
(``ValueError: Exceeds the limit (4300 digits)``) before 0.3.26.
"""
from __future__ import annotations

import io
import json
import random
import re
import sys
from contextlib import redirect_stdout
from fractions import Fraction
from pathlib import Path

import pytest

from cadl.cli import main
from cadl.games_eval import exact
from cadl.stochastic import decimal_text, fraction_text, int_text
from cadl.stochastic_eval import exact_text

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "games_grounding_robot_delivery.cadl"


@pytest.mark.parametrize("digits", [1, 2, 3613, 3614, 4300, 4301, 9114, 30000])
def test_int_text_is_str_for_any_size(digits):
    rng = random.Random(digits)
    n = rng.randrange(10 ** (digits - 1), 10 ** digits)
    text = int_text(n)
    assert len(text) == digits and text[0] != "0"
    # compare with str in pieces that str accepts
    for start in range(0, digits, 3000):
        stop = min(digits, start + 3000)
        assert text[start:stop] == str(n // 10 ** (digits - stop) % 10 ** (stop - start)).rjust(
            stop - start, "0")
    assert int_text(-n) == "-" + text


def test_int_text_keeps_inner_zeros():
    assert int_text(10 ** 5000) == "1" + "0" * 5000
    assert int_text(10 ** 5000 + 7) == "1" + "0" * 4999 + "7"
    assert int_text(0) == "0"


def test_exact_writers_accept_long_fractions():
    q = Fraction(10 ** 5000 + 1, 3 ** 9000)
    text = fraction_text(q)
    assert text == exact(q) == exact_text(q)
    num, den = text.split("/")
    assert len(num) == 5001 and len(den) == len(int_text(3 ** 9000))
    dec = decimal_text(Fraction(10 ** 5000 + 1, 2 ** 20))
    whole, frac = dec.split(".")
    assert whole == int_text((10 ** 5000 + 1) // 2 ** 20)
    assert Fraction(int(frac), 10 ** len(frac)) == Fraction((10 ** 5000 + 1) % 2 ** 20, 2 ** 20)


def test_game_check_with_long_exact_results(tmp_path):
    # the example with 960 delivery attempts instead of 3: some exact value
    # has a numerator or denominator of more than 4300 digits (about 20 s)
    text = EXAMPLE.read_text()
    assert text.count('"[0..3]"') == 1
    text = text.replace('"[0..3]"', '"[0..960]"').replace("attempts < 3", "attempts < 960") \
               .replace("attempts == 3", "attempts == 960")
    path = tmp_path / "long.cadl"
    path.write_text(text)
    out = io.StringIO()
    with redirect_stdout(out):
        status = main(["game-check", str(path), "--format", "json"])
    assert status == 0
    data = json.loads(out.getvalue())
    longest = max(len(part) for s in re.findall(r'"(-?\d+(?:/\d+)?)"', out.getvalue())
                  for part in s.lstrip("-").split("/"))
    assert longest > 4300
    assert data   # well-formed
    if hasattr(sys, "get_int_max_str_digits"):
        assert sys.get_int_max_str_digits() == 4300   # the limit of the process is unchanged
