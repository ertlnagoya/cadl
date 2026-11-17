"""CADL Metrics Code Generator — generates metrics collector."""

from __future__ import annotations

from typing import List

from ..ast_nodes import MetricDef
from .emitter import snake_case


def generate_metrics_module(metrics: List[MetricDef], schedules: dict | None = None,
                            run: dict | None = None) -> str:
    """Generate the metrics.py module with a MetricsCollector class.

    ``schedules`` maps the id of a metric with ``schedule:`` (amendment 0.1,
    Appendix K, K.5) to its entries, each ``(from, target)`` where `from` is
    ``{"period": k}``, ``{"at": text}``, ``{"effective": dict or None}`` for
    an entry from this version, or None for an entry from another version.
    Its target check then takes the time and the time base (K.11).

    ``run`` is given for a file that declares amendment 0.1 with an
    ``amendment:`` block (from cadl 0.3.35): ``{"version": the sos.version,
    "windows": metric -> the window of measure: in seconds or None,
    "schedules": metric -> [(from as written, target)]}``. Every target
    check then also takes ``versions``, the versions of the run, and judges
    as `cadl metrics-eval` does (K.11, "Metric schedules and
    measurements")."""
    if not metrics:
        return _empty_module()
    schedules = schedules or {}

    lines = []
    lines.append('"""Generated metrics collector for CADL SoS definition."""')
    lines.append("")
    lines.append("from __future__ import annotations")
    lines.append("")
    lines.append("from cadl.codegen.runtime_support import MetricsCollectorBase")
    lines.append("")
    lines.append("")
    if any(m.id in schedules for m in metrics) or run is not None:
        lines += _SCHEDULE_HELPER
    if run is not None:
        lines += _RUN_HELPER
    lines.append("class MetricsCollector(MetricsCollectorBase):")
    lines.append('    """Collects and evaluates SoS performance metrics."""')
    lines.append("")

    for metric in metrics:
        method_name = f"compute_{snake_case(metric.id)}"
        lines.append(f"    def {method_name}(self, ctx) -> float:")
        lines.append(f'        """Metric: {metric.id}')
        if metric.formula:
            lines.append(f"")
            lines.append(f"        Formula: {metric.formula}")
        if metric.target:
            lines.append(f"        Target: {metric.target}")
        lines.append(f'        """')
        if metric.formula:
            py_formula = _compile_formula(metric.formula)
            if py_formula:
                lines.append(f"        return {py_formula}")
            else:
                lines.append(f"        # formula: {metric.formula}")
                lines.append(f"        raise NotImplementedError('{method_name}')")
        else:
            lines.append(f"        raise NotImplementedError('{method_name}')")
        lines.append("")

    # Target check methods
    for metric in metrics:
        if run is not None:
            lines += _run_check(metric, schedules.get(metric.id), run)
        elif metric.id in schedules:
            lines += _scheduled_check(metric, schedules[metric.id])
        elif metric.target:
            method_name = f"check_{snake_case(metric.id)}_target"
            lines.append(f"    def {method_name}(self, value: float) -> bool:")
            lines.append(f'        """Check if {metric.id} meets target: {metric.target}."""')
            py_target = _compile_target(metric.target)
            if py_target:
                lines.append(f"        return {py_target}")
            else:
                # not judged (None): a target that cannot be compiled is never
                # met silently (up to cadl 0.3.33 this returned True)
                lines.append(f"        return None  # could not compile target: {metric.target}")
            lines.append("")

    content = "\n".join(lines)
    if not content.endswith("\n"):
        content += "\n"
    return content


def _compile_formula(formula: str) -> str | None:
    """Try to compile a formula string to Python source."""
    try:
        from ..parser import parse_expr
        from .expr_compiler import CompilerContext, expr_to_python
        expr = parse_expr(formula)
        ctx = CompilerContext()
        return expr_to_python(expr, ctx)
    except Exception:
        return None


def _compile_target(target: str) -> str | None:
    """Compile a target string like '>= 0.95' to a Python expression using 'value'."""
    import re
    m = re.match(r'([<>=!]+)\s*(.+)', target.strip())
    if not m:
        return None
    op, val = m.group(1), m.group(2).strip()
    # Try to parse val as a number or duration
    try:
        float(val)
        return f"value {op} {val}"
    except ValueError:
        pass
    # Try as duration (e.g., "30min") — convert to seconds for comparison
    m2 = re.match(r'(\d+(?:\.\d+)?)(ms|s|min|h)$', val)
    if m2:
        amount, unit = float(m2.group(1)), m2.group(2)
        unit_to_seconds = {"ms": 0.001, "s": 1.0, "min": 60.0, "h": 3600.0}
        seconds = amount * unit_to_seconds.get(unit, 1.0)
        return f"value {op} {seconds}"
    return None


_SCHEDULE_HELPER = [
    "def _from_time(when, period, origin, epoch):",
    '    """The time stamp at which a schedule entry applies, or None when the',
    '    time base has nothing for it (Appendix K, K.11)."""',
    "    import datetime as _dt",
    "    from fractions import Fraction",
    "    if when is None:",
    "        return None                    # an entry from another version",
    "    (form, value), = when.items()",
    "    if form == 'effective':           # an entry from this version",
    "        if value is None:",
    "            return float('-inf')",
    "        return _from_time(value, period, origin, epoch)",
    "    if form == 'period':",
    "        if period is None:",
    "            return None",
    "        return Fraction(origin) + (int(value) - 1) * Fraction(period)",
    "    if not epoch:",
    "        return None",
    "    try:",
    "        t = _dt.datetime.fromisoformat(value)",
    "    except ValueError:",
    "        t = _dt.datetime.combine(_dt.date.fromisoformat(value), _dt.time())",
    "    if t.tzinfo is None:",
    "        t = t.replace(tzinfo=_dt.timezone.utc)",
    "    d = t - _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)",
    "    return Fraction(d.days * 86400 + d.seconds) + Fraction(d.microseconds, 1000000)",
    "",
    "",
]


def _lambda(target) -> str:
    py = _compile_target(str(target)) if target is not None else None
    return f"lambda value: {py}" if py else "None"


def _scheduled_check(metric: MetricDef, entries: list) -> list:
    """The target check of a metric with schedule: (Appendix K, K.11)."""
    name = f"check_{snake_case(metric.id)}_target"
    out = [
        f"    def {name}(self, value: float, t=None, period=None, origin=0, epoch=False):",
        f'        """Check {metric.id} against the target in force at time t (schedule:,',
        "        Appendix K, K.11): period and origin give the time base of",
        "        `period:` entries, epoch that time stamps are seconds since",
        "        1970-01-01T00:00:00Z. Returns None (not judged) when t is not given,",
        "        an entry cannot be placed, or the target in force cannot be compiled.",
        "        It assumes that its own version is in force at t: a host runs only the",
        "        code of the version in force (Appendix K, K.11). An entry from another",
        "        version cannot be placed here and gives None. The check is for the one",
        "        time t: whether the target changes inside a measurement window is for",
        '        the caller to check (cadl metrics-eval does)."""',
        "        if t is None:",
        "            return None",
        f"        current = {_lambda(metric.target)}",
    ]
    for when, target in entries:
        out += [
            f"        at = _from_time({when!r}, period, origin, epoch)",
            "        if at is None:",
            "            return None",
            "        if at <= t:",
            f"            current = {_lambda(target)}  # {str(target)}",
        ]
    out += [
        "        return None if current is None else current(value)",
        "",
    ]
    return out


_RUN_HELPER = [
    "class _NotPlaced(Exception):",
    '    """A schedule entry whose time cannot be placed: not judged."""',
    "",
    "",
    "def _data(version, key):",
    "    return version[key] if isinstance(version, dict) else getattr(version, key)",
    "",
    "",
    "def _sequence(versions, period, origin, epoch):",
    '    """The versions of the run (the modules version.py of the Python',
    "    target, or dicts with their names), in the order of the sequence:",
    "    [(name, effective time or None, effective, TARGETS, SCHEDULES)].",
    '    Raises ValueError when they do not form one (Appendix K, K.11)."""',
    "    seq = []",
    "    for k, v in enumerate(versions):",
    "        name, prev, eff = _data(v, 'VERSION'), _data(v, 'PREVIOUS'), _data(v, 'EFFECTIVE')",
    "        if any(name == x[0] for x in seq):",
    "            raise ValueError(f'two versions of the run have the version {name!r}')",
    "        if k and prev is not None and prev != seq[-1][0]:",
    "            raise ValueError(f'version {name} names {prev} as previous, but the version '",
    "                             f'before it in versions is {seq[-1][0]}; give the versions '",
    "                             f'in the order of the sequence')",
    "        if k and not eff:",
    "            raise ValueError(f'version {name} gives no effective:')",
    "        at = _from_time(eff, period, origin, epoch) if eff else None",
    "        if k and at is not None and seq[-1][1] is not None and not at > seq[-1][1]:",
    "            raise ValueError(f'version {name} does not take effect after version '",
    "                             f'{seq[-1][0]}')",
    "        seq.append((name, at, eff, _data(v, 'TARGETS'), _data(v, 'SCHEDULES')))",
    "    return seq",
    "",
    "",
    "def _entry_time(frm, seq, period, origin, epoch):",
    "    (form, value), = frm.items()",
    "    if form == 'version':",
    "        for k, x in enumerate(seq):",
    "            if x[0] == value:",
    "                if x[1] is None and k == 0 and not x[2]:",
    "                    return float('-inf')",
    "                if x[1] is None:",
    "                    raise _NotPlaced()",
    "                return x[1]",
    "        raise _NotPlaced()",
    "    at = _from_time(frm, period, origin, epoch)",
    "    if at is None:",
    "        raise _NotPlaced()",
    "    return at",
    "",
    "",
    "def _target_at(target, schedule, x, seq, period, origin, epoch):",
    "    current = target",
    "    for frm, tgt in schedule or []:",
    "        if _entry_time(frm, seq, period, origin, epoch) <= x:",
    "            current = tgt",
    "    return current",
    "",
    "",
    "def _judge_in_run(metric, own, value, t, window, versions, period, origin, epoch, checks):",
    '    """The target check of `metric` in version `own` for a measurement',
    "    whose window starts at t, as `cadl metrics-eval` judges it",
    "    (Appendix K, K.11): None (not judged) when the version in force at t",
    "    is another one, an entry cannot be placed, the target changes inside",
    "    the window (by an entry of the schedule or by a version that takes",
    "    effect there and gives another target or does not declare the",
    '    metric), or the target in force cannot be compiled."""',
    "    if t is None:",
    "        return None",
    "    timed = period is not None or epoch",
    "    seq = _sequence(versions, period, origin, epoch)",
    "    names = [x[0] for x in seq]",
    "    if own not in names:",
    "        raise ValueError(f'versions does not contain this version, {own}')",
    "    k = names.index(own)",
    "    if timed:",
    "        live = [j for j, x in enumerate(seq) if x[1] is None or x[1] <= t]",
    "        if not live:",
    "            raise ValueError(f'no version is in force at t={t}')",
    "        if live[-1] != k:",
    "            return None                # the host runs the version in force",
    "    try:",
    "        targets, schedules = seq[k][3], seq[k][4]",
    "        current = _target_at(targets.get(metric), schedules.get(metric), t, seq,",
    "                             period, origin, epoch)",
    "        if window is not None:",
    "            start, end = t, t + window",
    "            while True:",
    "                nxt = seq[k + 1][1] if timed and k + 1 < len(seq) else None",
    "                stop = end if nxt is None or nxt >= end else nxt",
    "                targets, schedules = seq[k][3], seq[k][4]",
    "                if metric not in targets:",
    "                    return None",
    "                here = _target_at(targets[metric], schedules.get(metric), start, seq,",
    "                                  period, origin, epoch)",
    "                if here != current:",
    "                    return None",
    "                for frm, tgt in schedules.get(metric) or []:",
    "                    at = _entry_time(frm, seq, period, origin, epoch)",
    "                    if start < at < stop and tgt != current:",
    "                        return None",
    "                if stop >= end:",
    "                    break",
    "                k, start = k + 1, stop",
    "    except _NotPlaced:",
    "        return None",
    "    check = checks.get(current) if isinstance(current, str) else None",
    "    return None if check is None else check(value)",
    "",
    "",
]


def _run_check(metric: MetricDef, entries: list | None, run: dict) -> list:
    """The target check of a metric in a file with an amendment: block: as
    up to cadl 0.3.34 without `versions`, and as `cadl metrics-eval` with
    them (Appendix K, K.11)."""
    name = f"check_{snake_case(metric.id)}_target"
    texts = [metric.target] + [tgt for _, tgt in run["schedules"].get(metric.id, [])]
    checks = {}
    for text in texts:
        if isinstance(text, str) and text not in checks:
            checks[text] = _lambda(text)
    window = run["windows"].get(metric.id)
    body = [
        f"    def {name}(self, value: float, t=None, period=None, origin=0, epoch=False,",
        "                  versions=None):",
        f'        """Check {metric.id} against its target (Appendix K, K.11).',
        "",
        "        With `versions`, the versions of the run in the order of the",
        "        sequence (the modules version.py of their packages, this one",
        "        among them), t is the start of a measurement window and the",
        "        check judges as `cadl metrics-eval` does: entries from another",
        "        version are placed, and it returns None (not judged) when this",
        "        version is not in force at t, an entry cannot be placed, the",
        f"        target changes inside the window of {window}s (None: an instant),",
        "        or the target cannot be compiled. `period`, `origin` and `epoch`",
        '        give the time base."""',
        "        if versions is not None:",
        f"            return _judge_in_run({metric.id!r}, {run['version']!r}, value, t, {window!r},",
        "                                 versions, period, origin, epoch, {",
    ]
    for text, fn in checks.items():
        body.append(f"                                     {text!r}: {fn},")
    body.append("                                 })")
    if entries is not None:
        old = _scheduled_check(metric, entries)
        # the body of the 0.3.34 check, after its docstring
        start = next(i for i, l in enumerate(old) if l.strip() == "if t is None:")
        body += old[start:]
    elif metric.target:
        py_target = _compile_target(metric.target)
        if py_target:
            body.append(f"        return {py_target}")
        else:
            body.append(f"        return None  # could not compile target: {metric.target}")
        body.append("")
    else:
        body += ["        return None", ""]
    return body


def _empty_module() -> str:
    return (
        '"""Generated metrics collector — no metrics defined."""\n'
        "\n"
        "from __future__ import annotations\n"
    )
