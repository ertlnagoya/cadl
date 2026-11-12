"""Mutation checks of the temporal parts of the Solidity norms monitor
(tests/test_norms_monitor_targets.py, Appendix D, D.5).

Each mutant of norms_monitor_base.sol runs the traces of one set of
contents (RELEASE, COMPARE, TWO: the boundary traces, N random traces and
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
 ("two-no-close-u", "two", "assign(mu, i, X_U, a < x ? a : x);", ""),
 ("two-tag-mask", "two", "(x >> 16 & f)", "(x >> 16)"),
 ("two-until-prefix", "two", "assignFrom(mu, i, firstAfter(mu, op.puSet, op.pu));", "assignFrom(mu, i, 0);"),
 ("two-no-pend", "two", "mu.pend = !hit && w & CUR_D != 0;", "mu.pend = false;"),
 ("two-no-push", "two", "if (w >> (128 + 3 * j) & 1 == 0) continue;", "continue;"),
 ("two-root", "two", "for (uint256 lvl = 1; lvl <= mu.h; lvl++) {", "for (uint256 lvl = 1; lvl < mu.h; lvl++) {"),
]
SETS = {"release": (T.RELEASE, 20261301), "compare": (T.COMPARE, 20261302), "two": (T.TWO, 20261303)}
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
