"""Mutation checks of the temporal parts of the Solidity norms monitor
(tests/test_norms_monitor_targets.py, Appendix D, D.5).

Each mutant of norms_monitor_base.sol runs the traces of one set of
contents (RELEASE, COMPARE, TWO, THREE, FOUR, NESTED, ZERO, MIX, DEEP, FIVE: the boundary traces, N random traces and
three long ones) on the EVM; it is killed when a trace differs from
`cadl norms-eval`. Needs the EVM of the tests (CADL_EVM_PYTHON, CADL_SOLC).

    python scripts/norms_monitor_mutations.py . OUTDIR N [mutant ...]
"""
import sys, os, random, json
from pathlib import Path
repo = sys.argv[1]
sys.path.insert(0, repo); sys.path.insert(0, repo + "/src")
os.chdir(repo)
import cadl.codegen.solidity.norms_monitor_sol as M
from cadl.codegen.solidity.norms_monitor_sol import generate_norms_monitor_sol, monitor_name
from tests import norms_targets_support as S
import tests.test_norms_monitor_targets as T
out = Path(sys.argv[2]); N = int(sys.argv[3])
MUT = [
 ("rel-break-q-at-p", "release", "(!w.qnSet || w.qn > w.ffi)) return true;", "(!w.qnSet || w.qn >= w.ffi)) return true;"),
 ("rel-close-strict", "release", "return i < w.tail && w.t[i] + d < lim;", "return i < w.tail && w.t[i] + d <= lim;"),
 ("rel-no-luq", "release", "if (mode == W_RELEASE && w.qnSet) {", "if (false && w.qnSet) {"),
 ("rel-no-cut", "release", "if (until && w.head < w.tail && w.jSet", "if (mode == W_UNTIL && w.head < w.tail && w.jSet"),
 ("rel-truenow", "release", "if (mode == W_RELEASE) return qv == F && pv == F;", "if (mode == W_RELEASE) return qv == F;"),
 ("cmp-no-dual", "compare", "bool f = ts >> X_F & 1 != 0;", "bool f = false;"),
 ("cmp-both-live", "compare", "return l ? (f ? C_BOTH : C_LIVE)", "return l ? (f ? C_LIVE : C_LIVE)"),
 ("cmp-u-as-d", "two", "if (c == X_U) return mark(K_U);", "if (c == X_U) return mark(K_D);"),
 ("two-no-close-u", "two", "            assign(mu, i, X_U, a < x ? a : x);\n            assign(mu, i, op.mode",
  "            assign(mu, i, op.mode"),
 ("two-tag-mask", "two", "| ((y & BITS) >> sh & kp);", "| ((y & BITS) >> sh);"),
 ("two-until-prefix", "two", "assignRange(mu, i, X_T, firstAfter(mu, op.puSet, op.pu), mu.tail);",
  "assignRange(mu, i, X_T, 0, mu.tail);"),
 ("two-no-pend", "two", "mu.pend = !hit && und;", "mu.pend = false;"),
 ("two-no-push", "two", "if (tv == 0) continue;", "continue;"),
 ("two-root", "two", "for (uint256 lvl = 1; lvl <= mu.h; lvl++) {", "for (uint256 lvl = 1; lvl < mu.h; lvl++) {"),
 # three and four operators: the states of 5^m pairs, the words of a node
 ("three-shift", "three", "uint256 sh = 2 * (1 + v) * 5 ** i;", "uint256 sh = 2 * v * 5 ** i;"),
 ("three-keep", "three", "return i == 0 ? KEEP0 : i == 1 ? KEEP1 : KEEP2;", "return i == 0 ? KEEP0 : KEEP1;"),
 ("three-no-push", "three", "if (tv == 0) continue;", "continue;"),
 ("three-leaf", "three", "if (si == X_D) p += (1 + c) * f;", "if (si == X_D) p += c * f;"),
 ("three-cur", "three", "uint256 internal constant CUR_T = 0x5014000000001405;",
  "uint256 internal constant CUR_T = 0x1405;"),
 ("four-word-move", "four", "if (w / st % 5 == 0) x[w] |= x[w + (1 + v) * st] & BITS;",
  "if (w / st % 5 == 0) x[w] |= x[w + v * st] & BITS;"),
 ("four-current", "four", "for (; w > 0; w /= 5) if (w % 5 > 1) return false;", "w;"),
 ("four-tag-word", "four", "x[i / 3] |= v << (250 + 2 * (i % 3));", "x[0] |= v << (250 + 2 * (i % 3));"),
 ("four-undecided", "four", "if (w / st % 5 != 0 && x[w] & BITS != 0) return true;",
  "if (w / st % 5 > 1 && x[w] & BITS != 0) return true;"),
 # nested operators: the values of each level fed in the order of the observations
 ("nest-split", "nested", "lv.uSet ? lv.u + 1 : 0", "lv.uSet ? lv.u : 0"),
 ("nest-no-iu", "nested", "if (inp.x[k] == X_U) { lv.uSet = true; lv.u = inp.e[k]; }", ""),
 ("nest-window-edge", "nested", "a[k] = firstTime(data, f, inp.s[k], data.obsT[inp.s[k]] - lv.d);",
  "a[k] = firstTime(data, f, inp.s[k], data.obsT[inp.s[k]] - lv.d + 1);"),
 ("nest-final-tau", "nested", "uint256 x = firstFrom(mu, (g <= n ? data.obsT[g] : t) - op.td);",
  "uint256 x = firstFrom(mu, t - op.td);"),
 ("nest-range-lo", "nested", "uint256 lo = firstFrom(mu, data.obsT[r.s[k]] - op.td);",
  "uint256 lo = firstFrom(mu, data.obsT[r.e[k]] - op.td);"),
 ("nest-oneg", "nested", "uint8 y = flip(op.oneg, r.x[k]);", "uint8 y = flip(false, r.x[k]);"),
 ("nest-now", "nested", "if (r.e[k] == r.n) now_ = y;", ""),
 ("nest-u-mark", "nested", "op.uSet = true;\n                    op.u = r.e[k];", ""),
 ("nest-old-u", "nested", "assign(mu, i, X_U, a < x ? a : x);\n        assign(mu, i, op.outer",
  "assign(mu, i, op.outer"),
 # three and four levels: runs between the levels
 ("deep-order", "deep", "if (y != (pass == 0 ? dec : X_U)) continue;", "if (y != (pass == 1 ? dec : X_U)) continue;"),
 ("deep-level-tau", "deep", "r = level(lv, data, r, g <= n ? data.obsT[g] : t);", "r = level(lv, data, r, t);"),
 ("deep-neg", "deep", "inp.x[k] = flip(lv.neg, inp.x[k]);", "inp.x[k] = flip(false, inp.x[k]);"),
 ("deep-dec-f2", "deep", "if (inp.x[k] == dec && inp.e[k] + 1 > f2) f2 = inp.e[k] + 1;", ""),
 # (not listed: "v = x" in segments, which lets a later undefined run override a decisive one,
 # survives; no trace of the tests nor of 4 x 10^4 random chains of a model has a decisive run
 # before an undefined run whose ranges overlap, and the check is kept)
 ("deep-old-u", "deep", "uint8 v = j < u1 ? X_U : dec == X_T ? X_F : X_T;", "uint8 v = dec == X_T ? X_F : X_T;"),
 ("deep-next", "deep", "if (a[k] > j) { if (a[k] < next) next = a[k]; continue; }", "if (a[k] > j) continue;"),
 ("deep-u-next", "deep", "uint256 next = j < u1 && u1 < f2 ? u1 : f2;", "uint256 next = f2;"),
 # (not listed: dropping the reset of lv.f at a new activation is equivalent: the candidates are
 # at n or later, and a value of a level at j depends only on the values below at j or later)
 # five operators: the values of the content in four words, nodes of 25 words
 ("five-digits", "five", "if (i >= 4) return (w >> (2 * (i - 4))) & 3 == c", "if (i >= 4) return (w >> (2 * (i - 3))) & 3 == c"),
 ("five-leaf-word", "five", "uint256 bits = (ts[e / 256] >> (e % 256) & 1)", "uint256 bits = (ts[0] >> (e % 256) & 1)"),
 # (not listed: reading the entry of cur in word 0, "ts[0] | ds[0]", is equivalent: that entry
 # takes the fifth operator as undecided, and a content decided and not true with an operator
 # undecided is so for every value of it)
 ("five-words", "five", "uint256 words = ops.length > 4 ? 1 << (2 * (ops.length - 4)) : 1;",
  "uint256 words = ops.length > 4 ? 2 : 1;"),
 ("five-entries", "five", "out[v * words + w] |= both;", "out[v * words] |= both;"),
]
SETS = {"release": (T.RELEASE, 20261301), "compare": (T.COMPARE, 20261302), "two": (T.TWO, 20261303),
        "three": (T.THREE, 20261304), "four": (T.FOUR, 20261305), "nested": (T.NESTED, 20261306),
        "zero": (T.ZERO, 20261307), "mix": (T.MIX, 20261308), "deep": (T.DEEP, 20261309),
        "five": (T.FIVE, 20261310)}
base = M.BASE.read_text()
names = sys.argv[4:]
for name, st, old, new in MUT:
    if names and name not in names: continue
    assert base.count(old) == 1, name
    blocks, seed = SETS[st]
    sos = T._small(blocks)
    rng = random.Random(seed + 7)
    traces = T.EDGES + [T._epoch_trace(rng) for _ in range(N)] + [T._epoch_trace(rng, 70) for _ in range(3)]
    refs = [S.reference(sos, l) for l in traces]
    mpath = out / name / "base.sol"; mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(base.replace(old, new))
    M.BASE = mpath
    c = sos.contracts[0]
    try:
        comp = S.compile_solidity(generate_norms_monitor_sol(sos, c), monitor_name(c.id), out / name)
    except AssertionError as e:
        print(name, "does not compile", flush=True); continue
    runs = [S.sol_run(l, r.get("quantities", [])) for l, r in zip(traces, refs)]
    raw = S.run_evm(comp, f"Monitor.sol:{monitor_name(c.id)}", runs, out / name)
    got = S.sol_results(raw, runs, [n.id for n in c.norms])
    bad = S.compare(refs, got)
    print(name, "killed" if bad else "SURVIVED", f"({len(bad)} of {len(traces)} traces differ)", flush=True)
