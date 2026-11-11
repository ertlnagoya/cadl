"""pytest plugin that records the cases of tests/test_norms_eval.py for
tests/test_norms_monitor.py.

Run as ``python -m pytest tests/test_norms_eval.py -p tests.norms_monitor_cases``
with ``CADL_NORMS_CASES`` set to a file name. Every call of `evaluate` in
that test module is recorded with the trace lines it was given: the text
of the monitor generated from the file, the models of the versions the
trace amends to, and the result or the error of `cadl norms-eval`. The
tests of the generated monitor replay the same traces and compare.
"""

from __future__ import annotations

import copy
import json
import os

_cases: list = []
_skipped: list = []


def _patch(module) -> None:
    from cadl.codegen.norms_monitor_gen import build_model, generate_norms_monitor_module
    from cadl.norms_eval import NormsEvalError, TraceError

    orig_parse, orig_evaluate = module.parse_trace, module.evaluate

    def parse_trace(lines, path=None):
        lines = list(lines)
        trace = orig_parse(lines, path=path)
        trace._lines = lines
        return trace

    def evaluate(sos, trace, contract_id=None, versions=None):
        lines = getattr(trace, "_lines", None)
        case = None
        if lines is not None:
            try:
                case = {
                    "source": generate_norms_monitor_module(sos),
                    "versions": {k: build_model(v) for k, v in (versions or {}).items()},
                    "lines": lines, "contract": contract_id,
                }
            except ValueError as e:        # no norms: 0.1, so no monitor
                _skipped.append(str(e))
        try:
            result = orig_evaluate(sos, trace, contract_id, versions)
        except (NormsEvalError, TraceError) as e:
            if case is not None:
                case["error"] = [type(e).__name__, str(e)]
                _cases.append(case)
            raise
        if case is not None:
            case["result"] = copy.deepcopy(result)
            _cases.append(case)
        return result

    module.parse_trace, module.evaluate = parse_trace, evaluate


def pytest_collection_finish(session) -> None:
    seen = set()
    for item in session.items:
        module = item.module
        if module.__name__ not in seen and hasattr(module, "evaluate") \
                and hasattr(module, "parse_trace"):
            seen.add(module.__name__)
            _patch(module)


def pytest_sessionfinish(session) -> None:
    out = os.environ.get("CADL_NORMS_CASES")
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"cases": _cases, "skipped": _skipped}, f)
