"""`cadl metrics-eval`: judge measured values of metrics against their
targets (Appendix K, K.5 and K.11, "Metric schedules and measurements").

A measurement covers the window ``[s, s + window)`` of the metric's
``measure:``; it is judged under the declaration of the metric in the
version in force at ``s``, against the target in force at ``s`` (the
metric's ``target:`` or the last entry of its ``schedule:`` from ``s`` on),
and is not judged when the target changes inside the window. The values
of one run (``value``, or with ``every:`` the ``samples``) are combined by
``aggregate:``, the runs by ``across_runs:``. The formula of the metric is
evaluated by the system that runs, not here.

Measurements (JSON Lines, one per line)::

    {"metric": "on_time_deliveries", "period": 2, "runs": [41, 44, 40, 43, 45],
     "values": {"count(orders)": 48}}
    {"metric": "blocked_time", "t": 7200, "value": 512}

``metric``   the id of the metric (required);
``t``        the start of the window, a time stamp of the run, or
``period``   the number of the period at whose start the window starts
             (needs the length of a period);
``value``    the value of one run, ``samples`` the values of one run at
             ``s``, ``s + every``, ... (with ``every:``), or ``runs`` one
             value (or list of samples) per run (with ``runs:``);
``values``   the values of the terms of the target, by their text;
``version``  the version in force at ``s`` (optional; it must agree with
             the effective times when there is a time base).

Numbers are exact. Durations are in seconds.
"""

from __future__ import annotations

import json
import math
import re
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from .ast_nodes import BinaryOp, DurationLiteral, FloatLiteral, IntLiteral, UnaryOp
from .norms_eval import (NormsEvalError, TimeBase, TraceError, VersionChain, _exact,
                         _fmt_t, _plain, loads_strict, version_info)

_KEYS = {"metric", "t", "period", "value", "samples", "runs", "values", "version"}
_OPS = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
        ">=": lambda a, b: a >= b, "==": lambda a, b: a == b, "!=": lambda a, b: a != b}
_TARGET_RE = re.compile(r"^\s*(<=|>=|==|!=|<|>)\s*(.+?)\s*$", re.S)
_UNIT = {"ms": Fraction(1, 1000), "s": Fraction(1), "min": Fraction(60), "h": Fraction(3600)}


class MetricsEvalError(NormsEvalError):
    pass


class _NotJudged(Exception):
    pass


def load_file(path) -> Any:
    from .parser import parse_file
    from .type_checker import type_check
    if not Path(path).exists():
        raise MetricsEvalError(f"File not found: {path}")
    try:
        sos = parse_file(path)
    except Exception as e:
        raise MetricsEvalError(f"Parse error: {path}: {e}") from None
    check = type_check(sos)
    if not check.ok:
        errors = "\n".join(f"  {e}" for e in check.errors)
        raise MetricsEvalError(f"{path} has {len(check.errors)} type-check error(s):\n{errors}")
    return sos


class _Version:
    """A version as `metrics-eval` uses it when the file is not part of a
    sequence of versions."""

    def __init__(self, sos: Any) -> None:
        self.name = getattr(sos, "version", None)
        self.sos = sos
        self.effective = None


def _declarations(sos: Any) -> dict:
    """metric id -> (target, schedule, measure) of a version; the schedule
    and measure only when the file declares amendment 0.1 (K-13)."""
    from .amendment import amendment_declared, analyse
    extra = {}
    if amendment_declared(sos) and getattr(sos, "amendment", None) is not None:
        extra = {m["id"]: m for m in analyse(sos).metrics}
    out = {}
    for m in sos.metrics:
        e = extra.get(m.id, {})
        out[m.id] = (m.target, e.get("schedule"), e.get("measure"))
    return out


def _seconds(q: Any) -> Fraction | None:
    return None if q is None else Fraction(q.value) / 1000


def _aggregate(how: str, values: list) -> Fraction:
    if how == "mean":
        return sum(values, Fraction(0)) / len(values)
    if how == "median":
        v = sorted(values)
        k = len(v) // 2
        return v[k] if len(v) % 2 else (v[k - 1] + v[k]) / 2
    if how == "min":
        return min(values)
    if how == "max":
        return max(values)
    if how == "sum":
        return sum(values, Fraction(0))
    return values[-1]                       # last


def _number(v: Any, what: str, where: str) -> Fraction:
    v = _exact(v)
    if isinstance(v, bool) or not isinstance(v, (int, Fraction)):
        raise TraceError(f"{where}: {what} must be a number")
    return Fraction(v)


def _key(text: str) -> str:
    """The text of a term as the target and the measurement write it,
    without white space."""
    return re.sub(r"\s+", "", text)


def _value_of(expr: Any, terms: dict) -> Fraction:
    from .unparse import expr_to_source
    if isinstance(expr, IntLiteral) and not isinstance(expr.value, bool):
        return Fraction(expr.value)
    if isinstance(expr, FloatLiteral):
        return Fraction(Decimal(expr.text if expr.text else repr(expr.value)))
    if isinstance(expr, DurationLiteral):
        return Fraction(expr.value) * _UNIT[expr.unit]
    if isinstance(expr, UnaryOp) and expr.op == "-":
        return -_value_of(expr.operand, terms)
    if isinstance(expr, BinaryOp) and expr.op in ("+", "-", "*", "/"):
        a, b = _value_of(expr.left, terms), _value_of(expr.right, terms)
        if expr.op == "+":
            return a + b
        if expr.op == "-":
            return a - b
        if expr.op == "*":
            return a * b
        if b == 0:
            raise _NotJudged("the target divides by zero")
        return a / b
    try:
        text = expr_to_source(expr)
    except Exception:
        raise _NotJudged("the target is not an arithmetic expression of numbers, durations "
                         "and terms") from None
    if _key(text) not in terms:
        raise _NotJudged(f"the measurement gives no value for '{text}' of the target")
    return terms[_key(text)]


def compare(target: Any, value: Fraction, terms: dict) -> tuple:
    """(met, the bound) of a target that is a comparison (K.11, item 4)."""
    from .parser import parse_expr
    m = _TARGET_RE.match(str(target)) if isinstance(target, str) else None
    if m is None:
        raise _NotJudged(f"the target {target!r} is not a comparison operator followed by "
                         f"an expression")
    try:
        expr = parse_expr(m.group(2))
    except Exception:
        raise _NotJudged(f"the target {target!r} does not parse") from None
    bound = _value_of(expr, terms)
    return _OPS[m.group(1)](value, bound), bound


class _Run:
    def __init__(self, files: list, chain: VersionChain | None, base: TimeBase | None) -> None:
        self.chain = chain
        self.base = base if base is not None and base.given else None
        self.versions = chain.infos if chain is not None else [_Version(files[0])]

    def version_at(self, s: Fraction, named: Any, where: str) -> int:
        chain = self.chain
        if chain is None:
            if named is not None:
                raise TraceError(f"{where}: 'version' needs the versions of a run: the file "
                                 f"does not declare 'amendment: 0.1' with an amendment: block")
            return 0
        if named is not None:
            k = chain.index(chain.check_name(named, where))
            if chain.timed and chain.by_time(s) != k:
                raise TraceError(
                    f"{where}: the measurement names version {named}, but by the effective "
                    f"times version {chain.infos[chain.by_time(s)].name} is in force at "
                    f"t={_fmt_t(s)}")
            return k
        if chain.timed:
            try:
                return chain.by_time(s)
            except TraceError as e:
                raise TraceError(f"{where}: {e}") from None
        return 0

    def entry_time(self, entry: dict) -> Fraction:
        (form, v), = entry["from"].items()
        if form == "version":
            if self.chain is None or v not in self.chain.names():
                raise _NotJudged(f"the schedule entry from version {v} cannot be placed: "
                                 f"version {v} is not a version of the run")
            k = self.chain.index(v)
            t = self.chain.times[k]
            if t is None and k == 0 and not self.chain.infos[0].effective:
                return Fraction(-(10 ** 30))
            if t is None:
                raise _NotJudged(f"the schedule entry from version {v} cannot be placed: "
                                 f"give the time base of the effective times")
            return t
        base = self.base or TimeBase()
        t = base.time_of(entry["from"])
        if t is None:
            what = "--period" if form == "period" else "--epoch"
            raise _NotJudged(f"the schedule entry from {form} {v} cannot be placed: give "
                             f"{what}")
        return t

    def judge(self, rec: dict, where: str) -> dict:
        metric = rec["metric"]
        if "t" in rec:
            s = _number(rec["t"], "'t'", where)
        else:
            k = rec["period"]
            if isinstance(k, bool) or not isinstance(k, int) or k < 1:
                raise TraceError(f"{where}: 'period' must be a positive integer")
            if self.base is None or self.base.period is None:
                raise TraceError(f"{where}: 'period' needs the length of a period (--period)")
            s = self.base.origin + (k - 1) * self.base.period
        vi = self.version_at(s, rec.get("version"), where)
        version = self.versions[vi]
        out = {"line": where, "metric": metric, "start": s, "version": version.name,
               "window": None, "value": None, "target": None, "target_from": None,
               "decision": None, "bound": None, "verdict": "not judged", "reason": ""}
        decl = _declarations(version.sos).get(metric)
        if decl is None:
            out["reason"] = f"version {version.name} has no metric '{metric}'"
            return out
        target, schedule, measure = decl
        measure = measure or {}
        window = _seconds(measure.get("window"))
        every = _seconds(measure.get("every"))
        out["window"] = window
        out["value"] = self.value(rec, measure, window, every, where)
        terms = {}
        for name, v in dict(rec.get("values") or {}).items():
            terms[_key(str(name))] = _number(v, f"the value of '{name}'", where)
        try:
            current, source, decision = target, "target", None
            for entry in schedule or []:
                t = self.entry_time(entry)
                if t <= s:
                    current, decision = entry["target"], entry.get("decision")
                    source = f"schedule from {_from_text(entry['from'])}"
            out["target"], out["target_from"], out["decision"] = current, source, decision
            if window is not None:
                for entry in schedule or []:
                    t = self.entry_time(entry)
                    if s < t < s + window and entry["target"] != current:
                        raise _NotJudged(
                            f"the target changes inside the window, at "
                            f"{_from_text(entry['from'])} (t={_fmt_t(t)})")
            if current is None:
                raise _NotJudged("the metric has no target")
            met, bound = compare(current, out["value"], terms)
            out["bound"] = bound
            out["verdict"] = "met" if met else "not met"
        except _NotJudged as e:
            out["reason"] = str(e)
        return out

    def value(self, rec: dict, measure: dict, window: Any, every: Any, where: str) -> Fraction:
        runs = measure.get("runs")
        if every is not None:
            count = math.ceil(window / every)
        given = [k for k in ("value", "samples", "runs") if k in rec]
        if len(given) != 1:
            raise TraceError(f"{where}: a measurement gives exactly one of 'value', 'samples' "
                             f"and 'runs'")
        if runs is not None:
            if given != ["runs"] or not isinstance(rec["runs"], list) or len(rec["runs"]) != runs:
                raise TraceError(f"{where}: the measure: of '{rec['metric']}' has runs: {runs}; "
                                 f"give 'runs' with {runs} entries")
            per_run = rec["runs"]
        elif given == ["runs"]:
            raise TraceError(f"{where}: the measure: of '{rec['metric']}' has no runs:; give "
                             f"'value' or 'samples'")
        else:
            per_run = [rec[given[0]]]
        values = []
        for r in per_run:
            if every is None:
                if isinstance(r, list) or given == ["samples"]:
                    raise TraceError(f"{where}: the measure: of '{rec['metric']}' has no "
                                     f"every:; a run gives one value")
                values.append(_number(r, "a value", where))
            else:
                if not isinstance(r, list) or len(r) != count:
                    raise TraceError(
                        f"{where}: the measure: of '{rec['metric']}' takes {count} samples per "
                        f"run (every: within window:); a run gives a list of {count} numbers")
                values.append(_aggregate(measure.get("aggregate", "mean"),
                                         [_number(x, "a sample", where) for x in r]))
        return _aggregate(measure.get("across_runs", "mean"), values) if runs else values[0]


def _from_text(f: dict) -> str:
    (form, v), = f.items()
    return f"{form} {v}"


def evaluate(files: list, records: list, chain: VersionChain | None = None,
             base: TimeBase | None = None) -> dict:
    """Judge measurements; `records` are (where, dict) pairs."""
    run = _Run(files, chain, base)
    results = []
    for where, rec in records:
        if not isinstance(rec, dict):
            raise TraceError(f"{where}: a measurement must be a JSON object")
        unknown = sorted(set(rec) - _KEYS)
        if unknown:
            raise TraceError(f"{where}: unknown key(s) {', '.join(unknown)}")
        if not isinstance(rec.get("metric"), str):
            raise TraceError(f"{where}: 'metric' must name a metric")
        if ("t" in rec) == ("period" in rec):
            raise TraceError(f"{where}: give exactly one of 't' and 'period'")
        if "values" in rec and not isinstance(rec["values"], dict):
            raise TraceError(f"{where}: 'values' must be an object")
        results.append(run.judge(rec, where))
    counts = {k: sum(1 for r in results if r["verdict"] == k)
              for k in ("met", "not met", "not judged")}
    return _plain({"versions": [v.name for v in run.versions], "measurements": results,
                   "counts": counts, "ok": counts["not met"] == 0})


def evaluate_files(path, measurements_path, version_paths=(), base: TimeBase | None = None
                   ) -> dict:
    from .norms_eval import VersionChain
    sos = load_file(path)
    version_paths = list(version_paths or ())
    from .amendment import amendment_declared
    raw = getattr(sos, "amendment", None)
    has_block = amendment_declared(sos) and raw is not None and raw.has_block
    chain = None
    if has_block or version_paths:
        infos = [version_info(sos, path)] + [version_info(load_file(p), p)
                                             for p in version_paths]
        chain = VersionChain(infos, base)
    if not Path(measurements_path).exists():
        raise MetricsEvalError(f"File not found: {measurements_path}")
    records = []
    with open(measurements_path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            if not line.strip():
                continue
            where = f"line {lineno}"
            try:
                records.append((where, loads_strict(line)))
            except ValueError as e:
                raise TraceError(f"{where}: not JSON ({e})") from None
    result = evaluate([sos], records, chain, base)
    result["file"] = str(path)
    result["measurements_file"] = str(measurements_path)
    return result


def format_text(result: dict) -> str:
    lines = [f"Metrics of {result['file']} (versions: "
             f"{', '.join(str(v) for v in result['versions'])})", ""]
    for r in result["measurements"]:
        window = (f"[{_fmt_t(r['start'])}, {_fmt_t(r['start'] + r['window'])})"
                  if r["window"] is not None else f"at t={_fmt_t(r['start'])}")
        lines.append(f"  {r['line']}: {r['metric']} {window}, version {r['version']}")
        if r["value"] is not None:
            lines.append(f"    value {_fmt_num(r['value'])}")
        if r["target"] is not None:
            bound = f" (= {_fmt_num(r['bound'])})" if r["bound"] is not None else ""
            dec = f", decision {r['decision']}" if r["decision"] else ""
            lines.append(f"    target {r['target']}{bound}, from the {r['target_from']}{dec}")
        lines.append(f"    {r['verdict']}" + (f": {r['reason']}" if r["reason"] else ""))
    c = result["counts"]
    lines += ["", f"Result: {c['met']} met, {c['not met']} not met, {c['not judged']} not judged"]
    return "\n".join(lines)


def _fmt_num(v: Any) -> str:
    return format(float(v), ".12g")
