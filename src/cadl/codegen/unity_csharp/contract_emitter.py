"""Per-contract C# emitter for the SoS-DSL Unity codegen target.

Produces three files per contract:

  - ``<Cls>State.cs``    — public enum of lifecycle states.
  - ``<Cls>Monitors.cs`` — declarative monitor evaluator (one method
    per monitor id, plus a Tick / OnEvent dispatcher).
  - ``<Cls>Contract.cs`` — IContractInstance implementation: holds
    state, fires transitions on event match or tick deadlines, lifts
    on_violation, owns its monitor evaluator instance.

Naming convention
-----------------
A contract id like ``DELIVERY_SLA`` becomes the C# identifier
``DeliverySla`` (PascalCase, single-token). Spec users who care about
exact naming should pick contract ids that already PascalCase cleanly.
"""

from __future__ import annotations

import re
from typing import Iterable, Tuple

from ...ast_nodes import ContractDef, LifecycleSpec, MonitorDef


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------

def _csharp_class_name(contract_id: str) -> str:
    """Convert a CADL contract id into a PascalCase C# class name.

    DELIVERY_SLA -> DeliverySla
    delivery-sla -> DeliverySla
    Delivery_Sla -> DeliverySla
    """
    parts = re.split(r'[^A-Za-z0-9]+', contract_id)
    parts = [p for p in parts if p]
    if not parts:
        return "Contract"
    return ''.join(p[:1].upper() + p[1:].lower() for p in parts)


def _csharp_member(name: str) -> str:
    """Convert an arbitrary id into a safe C# member identifier."""
    s = re.sub(r'[^A-Za-z0-9_]', '_', name)
    if not s or s[0].isdigit():
        s = '_' + s
    return s


def _csharp_string(s: str) -> str:
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"').replace(
        '\n', '\\n').replace('\r', '\\r').replace('\t', '\\t') + '"'


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def emit_contract(
    contract: ContractDef,
    namespace: str = "CADL.SosDsl",
    regime_bound: bool = False,
) -> Iterable[Tuple[str, str]]:
    """`regime_bound`: some regime of the file does not have the contract in
    force (regimes:, Appendix A, A.7.1); the instance then follows the
    current regime of a CadlRegimes object (see regimes_file)."""
    cls = _csharp_class_name(contract.id)
    yield f"{cls}State.cs", _emit_state_enum(contract, cls, namespace)
    text = _emit_monitors(contract, cls, namespace)
    if regime_bound:
        text = _monitors_with_shift(contract, text)
    yield f"{cls}Monitors.cs", text
    text = _emit_contract_class(contract, cls, namespace)
    if regime_bound:
        text = _contract_with_regimes(text, cls, bool(contract.monitors))
    yield f"{cls}Contract.cs", text


# ---------------------------------------------------------------------------
# Regimes (Appendix A, A.7.1; F.5, "Contracts not in force")
# ---------------------------------------------------------------------------

def _replace_once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


def _monitors_with_shift(contract: ContractDef, text: str) -> str:
    """Shift(d): the periodic monitors resume on the clock of the contract."""
    periodic = [m for m in contract.monitors
                if m.sampling and m.sampling.kind == "periodic" and m.sampling.period_ms]
    body = "".join(f"            _last_{_csharp_member(m.id)} += d;\n" for m in periodic)
    method = ("        // The contract was not in force for d ms: its clock stopped.\n"
              "        public void Shift(long d) {\n" + body + "        }\n\n")
    if not contract.monitors:
        return _replace_once(text, "        public void Tick(long nowMs, ContractRuntime rt) { }\n",
                             "        public void Tick(long nowMs, ContractRuntime rt) { }\n"
                             "        public void Shift(long d) { }\n")
    return _replace_once(text, "        // Fire a target transition (by id)",
                         method + "        // Fire a target transition (by id)")


def _contract_with_regimes(text: str, cls: str, has_monitors: bool) -> str:
    """The contract class of a contract that some regime does not have in
    force: while the current regime does not have it in force, events and
    ticks are not seen and its clock stops (deadlines and periodic
    monitors are moved on by the time not in force when it returns)."""
    text = _replace_once(text, f"public sealed class {cls}Contract : IContractInstance {{",
                         f"public sealed class {cls}Contract : IContractInstance, IRegimeBound {{")
    text = _replace_once(
        text,
        f"        public {cls}Contract(string instanceId, ContractRuntime rt, long nowMs = 0) {{\n",
        "        // Not in force in the current regime since _outSince (A.7.1, F.5).\n"
        "        private bool _out;\n"
        "        private long _outSince;\n"
        "\n"
        "        /// <summary>Whether the current regime has the contract in force.</summary>\n"
        "        public bool InForce => !_out;\n"
        "\n"
        "        /// <summary>\n"
        "        /// The instance follows the current regime of <paramref name=\"regimes\"/>,\n"
        "        /// or starts in the initial regime when it is null; register it with\n"
        "        /// the runtime so that CadlRegimes.Set reaches it.\n"
        "        /// </summary>\n"
        f"        public {cls}Contract(string instanceId, ContractRuntime rt, long nowMs = 0,\n"
        f"                {' ' * len(cls)}CadlRegimes regimes = null) {{\n"
        "            _out = !(regimes != null ? regimes.InForce(ContractId)\n"
        "                     : CadlRegimes.InForceIn(CadlRegimes.Initial, ContractId));\n"
        "            _outSince = nowMs;\n")
    text = _replace_once(
        text,
        "        public void HandleEvent(in PortEvent ev, ContractRuntime rt) {\n"
        "            if (Closed) return;\n",
        "        /// <summary>Called by CadlRegimes.Set at time tMs.</summary>\n"
        "        public void SetInForce(bool inForce, long tMs) {\n"
        "            if (inForce != _out) return;\n"
        "            if (!inForce) {\n"
        "                _out = true;\n"
        "                _outSince = tMs;\n"
        "                return;\n"
        "            }\n"
        "            _out = false;\n"
        "            long d = tMs - _outSince;\n"
        "            if (d <= 0) return;\n"
        "            for (int i = 0; i < _deadlines.Count; i++) {\n"
        "                var x = _deadlines[i];\n"
        "                x.DeadlineMs += d;\n"
        "                _deadlines[i] = x;\n"
        "            }\n"
        + ("            _monitors.Shift(d);\n" if has_monitors else "")
        + "        }\n"
        "\n"
        "        public void HandleEvent(in PortEvent ev, ContractRuntime rt) {\n"
        "            if (Closed || _out) return;\n")
    text = _replace_once(
        text,
        "        public void Tick(long nowMs, ContractRuntime rt) {\n"
        "            if (Closed) return;\n",
        "        public void Tick(long nowMs, ContractRuntime rt) {\n"
        "            if (Closed || _out) return;\n")
    return text


# ---------------------------------------------------------------------------
# Versions of a run (Appendix K, K.11)
# ---------------------------------------------------------------------------

def emit_version_body(contract: ContractDef, k: int, namespace: str,
                      regimes: bool = False) -> Iterable[Tuple[str, str]]:
    """The body of the contract in version k: the classes of
    emit_contract named <Cls>V<k>, with a constructor that takes over a
    running instance (its state, the time at which its deadlines were
    armed, the last times of its periodic monitors) and what the versioned
    class reads of it (IVersionBody). `regimes`: a version of the run binds
    regimes; the body also has Shift(d), which moves its clock on by the
    time the contract was not in force (F.5)."""
    cls = f"{_csharp_class_name(contract.id)}V{k}"
    yield f"{cls}State.cs", _emit_state_enum(contract, cls, namespace)
    periodic = [m for m in contract.monitors
                if m.sampling and m.sampling.kind == "periodic" and m.sampling.period_ms]
    mon = _emit_monitors(contract, cls, namespace)
    get = "".join(f"            d[{_csharp_string(m.id)}] = _last_{_csharp_member(m.id)};\n"
                  for m in periodic)
    put = "".join(f"            if (d.TryGetValue({_csharp_string(m.id)}, out var v{i})) "
                  f"_last_{_csharp_member(m.id)} = v{i};\n" for i, m in enumerate(periodic))
    methods = ("        public Dictionary<string, long> GetLast() {\n"
               "            var d = new Dictionary<string, long>();\n" + get +
               "            return d;\n        }\n"
               "        public void SetLast(IDictionary<string, long> d) {\n" + put + "        }\n")
    if not contract.monitors:
        mon = _replace_once(mon, "        public void Tick(long nowMs, ContractRuntime rt) { }\n",
                            "        public void Tick(long nowMs, ContractRuntime rt) { }\n" + methods)
        mon = mon.replace("// <auto-generated />\n", "// <auto-generated />\nusing System.Collections.Generic;\n", 1)
    else:
        mon = _replace_once(mon, "        // Fire a target transition (by id)",
                            methods + "\n        // Fire a target transition (by id)")
    if regimes:
        mon = _monitors_with_shift(contract, mon)
    yield f"{cls}Monitors.cs", mon
    text = _emit_contract_class(contract, cls, namespace)
    text = _replace_once(text, f"public sealed class {cls}Contract : IContractInstance {{",
                         f"public sealed class {cls}Contract : IContractInstance, IVersionBody {{")
    text = _replace_once(text, f"        public {cls}Contract(string instanceId, ContractRuntime rt, long nowMs = 0) {{\n",
        "        // when the deadlines of the current state were armed\n"
        "        private long _armedAt;\n"
        "        public long ArmedAt => _armedAt;\n"
        "        public string StateName => _state.ToString();\n"
        "        public Dictionary<string, long> MonitorLast() => "
        + ("_monitors.GetLast();\n" if contract.monitors else "new Dictionary<string, long>();\n")
        + "\n"
        "        /// <summary>Takes over a running instance of another version (F.6):\n"
        "        /// its state, the time at which the deadlines of that state were armed\n"
        "        /// (so the time elapsed is kept), and the last times of its periodic\n"
        "        /// monitors. Throws when the state is not a state of this version.</summary>\n"
        f"        public {cls}Contract(string instanceId, string state, long armedAt,\n"
        "                IDictionary<string, long> last) {\n"
        "            InstanceId = instanceId;\n"
        f"            if (!Enum.TryParse<{cls}State>(state, out _state))\n"
        "                throw new ArgumentException(\"the state \" + state + \" is not a state of the lifecycle in this version\");\n"
        + (f"            _monitors = new {cls}Monitors(this);\n            _monitors.SetLast(last);\n"
           if contract.monitors else "")
        + "            if (IsTerminal(_state)) Closed = true; else ArmDeadlinesFor(_state, armedAt);\n"
        "        }\n\n"
        f"        public {cls}Contract(string instanceId, ContractRuntime rt, long nowMs = 0) {{\n")
    text = _replace_once(text, f"        private void ArmDeadlinesFor({cls}State newState, long nowMs) {{\n",
                         f"        private void ArmDeadlinesFor({cls}State newState, long nowMs) {{\n"
                         "            _armedAt = nowMs;\n")
    if regimes:
        text = _replace_once(
            text, "        public string StateName => _state.ToString();\n",
            "        public string StateName => _state.ToString();\n"
            "        /// <summary>The contract was not in force for d ms: its clock stopped\n"
            "        /// (deadlines, the time they were armed, periodic monitors).</summary>\n"
            "        public void Shift(long d) {\n"
            "            _armedAt += d;\n"
            "            for (int i = 0; i < _deadlines.Count; i++) {\n"
            "                var x = _deadlines[i];\n"
            "                x.DeadlineMs += d;\n"
            "                _deadlines[i] = x;\n"
            "            }\n"
            + ("            _monitors.Shift(d);\n" if contract.monitors else "")
            + "        }\n")
    yield f"{cls}Contract.cs", text


def versioned_class(contract_id: str, names: list, rules: list, bodies: list,
                    namespace: str = "CADL.SosDsl") -> str:
    """<Cls>Contract of a run with versions: it follows the version in force
    that the host sets with SetVersion, with the transitional rule of the
    contract in each version (K.11)."""
    cls = _csharp_class_name(contract_id)
    n_lit = ", ".join(_csharp_string(x) for x in names)
    r_lit = ", ".join(_csharp_string(r["rule"]) for r in rules)
    s_lit = ", ".join("new string[] { " + ", ".join(_csharp_string(x) for x in r["states"]) + " }"
                      for r in rules)
    create = "".join(f"                case {k}: return new {cls}V{k}Contract(id, rt, nowMs);\n"
                     for k in bodies)
    take = "".join(f"                case {k}: return new {cls}V{k}Contract(id, b.StateName, b.ArmedAt, b.MonitorLast());\n"
                   for k in bodies)
    return f"""// <auto-generated>
//   Source: CADL contract {contract_id}, versions {', '.join(names)}
// </auto-generated>
using System;
using System.Collections.Generic;

namespace {namespace} {{

    /// <summary>
    /// The contract {contract_id} through the versions of a run (Appendix K,
    /// K.11). The instance follows the version in force at its construction;
    /// the host calls SetVersion(name, tMs) when a version takes effect (it
    /// computes that from the effective times). The transitional rule of the
    /// contract in that version applies: keep leaves the instance on its
    /// version, switch moves it to the new one at once, and switch with
    /// in_states at the first event or tick at which its state is one of them
    /// (a waiting switch is dropped when a later version takes effect). A
    /// moved instance keeps its state, the time at which the deadlines of that
    /// state were armed and the last times of its periodic monitors; the
    /// event Lifecycle with TransitionId "<version>" reports the move.
    /// </summary>
    public sealed class {cls}Contract : IContractInstance {{
        private static readonly string[] Names = {{ {n_lit} }};
        private static readonly string[] Rules = {{ {r_lit} }};
        private static readonly string[][] States = {{ {s_lit} }};

        private IContractInstance _body;
        private int _current, _follows, _pending = -1;

        public string ContractId => {_csharp_string(contract_id)};
        public string InstanceId {{ get; }}
        public bool Closed => _body.Closed;
        /// <summary>The version the instance follows.</summary>
        public string Version => Names[_follows];
        public IContractInstance Body => _body;

        public {cls}Contract(string instanceId, ContractRuntime rt, long nowMs = 0,
                {' ' * len(cls)}string version = null) {{
            InstanceId = instanceId;
            _current = _follows = version == null ? 0 : Index(version);
            _body = Create(_follows, instanceId, rt, nowMs);
        }}

        private static int Index(string name) {{
            int k = Array.IndexOf(Names, name);
            if (k < 0) throw new ArgumentException("not a version of the run: " + name);
            return k;
        }}

        private static IContractInstance Create(int k, string id, ContractRuntime rt, long nowMs) {{
            switch (k) {{
{create}                default: throw new ArgumentException("no body of version " + k);
            }}
        }}

        private static IContractInstance Take(int k, string id, IVersionBody b) {{
            switch (k) {{
{take}                default: throw new ArgumentException("no body of version " + k);
            }}
        }}

        private string State => ((IVersionBody)_body).StateName;

        private void SwitchTo(int v, ContractRuntime rt, long tMs) {{
            string from = State;
            _body = Take(v, InstanceId, (IVersionBody)_body);
            _follows = v;
            _pending = -1;
            rt.Emit(new ContractEvent {{
                Kind = ContractEventKind.Lifecycle, ContractId = ContractId,
                InstanceId = InstanceId, TransitionId = "<version>", FromState = from,
                ToState = State, TMs = tMs, Cause = "version " + Names[v],
            }});
        }}

        private void CheckPending(ContractRuntime rt, long tMs) {{
            if (_pending >= 0 && !Closed && Array.IndexOf(States[_pending], State) >= 0)
                SwitchTo(_pending, rt, tMs);
        }}

        /// <summary>The version is in force from tMs on (K.11).</summary>
        public void SetVersion(string version, long tMs, ContractRuntime rt) {{
            int k = Index(version);
            // after the end of the instance only the name is checked, as cadl norms-eval
            if (Closed) {{ _current = k; return; }}
            if (k < _current)
                throw new ArgumentException("version " + version + " comes before the version in force");
            CheckPending(rt, tMs);
            for (int v = _current + 1; v <= k; v++) {{
                _pending = -1;
                if (Rules[v] == "keep") continue;
                if (States[v].Length == 0 || Array.IndexOf(States[v], State) >= 0) SwitchTo(v, rt, tMs);
                else _pending = v;
            }}
            _current = k;
        }}

        public void HandleEvent(in PortEvent ev, ContractRuntime rt) {{
            CheckPending(rt, ev.TMs);
            _body.HandleEvent(ev, rt);
        }}

        public void Tick(long nowMs, ContractRuntime rt) {{
            CheckPending(rt, nowMs);
            _body.Tick(nowMs, rt);
        }}

        public IDictionary<string, object> WorldSnapshot() => _body.WorldSnapshot();
    }}
}}
"""


def versioned_class_regimes(contract_id: str, names: list, rules: list, bodies: list,
                            namespace: str = "CADL.SosDsl") -> str:
    """<Cls>Contract of a run with versions of which some bind regimes
    (Appendix K, K.11, "Versions and regimes"): versioned_class that also
    follows the current regime of a CadlRegimes object (regimes_versions_file)."""
    text = versioned_class(contract_id, names, rules, bodies, namespace)
    cls = _csharp_class_name(contract_id)
    head, body = text.split("    /// <summary>\n    /// The contract", 1)
    tail_from = body.index("        private static IContractInstance Create(")
    tail_to = body.index("        private string State =>")
    create_take = body[tail_from:tail_to]
    pad = " " * len(cls)
    cls_text = f"""    /// <summary>
    /// The contract {contract_id} through the versions of a run (Appendix K,
    /// K.11) whose versions bind regimes (Appendix A, A.7.1): versioned_class
    /// with regimes. The host calls SetVersion(name, tMs) when a version takes
    /// effect and CadlRegimes.Set(regime, tMs) when the regime changes; at a
    /// time at which both change, Set comes first. The contract is in force
    /// by the regime bindings of the version in force. The instance starts
    /// (the body of the version in force is created, and reports its
    /// opening) at the first time at which the contract is in force. While it
    /// is not in force the instance sees no event and no tick, and its clock
    /// stops: when it is in force again, its deadlines, the time they were
    /// armed and its periodic monitors are moved on by the time not in force
    /// (F.5, "Contracts not in force"). A version that takes effect while the
    /// contract is not in force reaches the instance when it is in force
    /// again, before anything else: the transitional rule is applied then,
    /// on the state the instance had (it does not change meanwhile), in the
    /// order in which the versions took effect; the Lifecycle event of a
    /// switch it makes has the time at which the version took effect (the
    /// tMs of SetVersion), and a waiting switch with in_states: has the time
    /// at which it happens. A regime that is current at
    /// some time must be a regime of every version that the instance has had
    /// in force (a version without regimes: has only the run in no regime);
    /// SetVersion and CadlRegimes.Set throw otherwise.
    /// </summary>
    public sealed class {cls}Contract : IContractInstance, IRegimeBound {{
        private static readonly string[] Names = {{ {", ".join(_csharp_string(x) for x in names)} }};
        private static readonly string[] Rules = {{ {", ".join(_csharp_string(r["rule"]) for r in rules)} }};
        private static readonly string[][] States = {{ {", ".join("new string[] { " + ", ".join(_csharp_string(x) for x in r["states"]) + " }" for r in rules)} }};

        private IContractInstance _body;     // null until the contract is first in force
        private int _current, _follows, _pending = -1;
        private int _vin;                    // the version in force
        private string _regime;              // the current regime (null: in no regime)
        private bool _out;
        private long _outSince;
        private readonly ContractRuntime _rt;
        private readonly List<int> _used = new List<int>();
        private readonly List<string> _seen = new List<string>();
        // the versions that took effect while the contract was not in force,
        // and the times at which they took effect
        private readonly List<int> _waiting = new List<int>();
        private readonly List<long> _waitingT = new List<long>();

        public string ContractId => {_csharp_string(contract_id)};
        public string InstanceId {{ get; }}
        public bool Closed => _body != null && _body.Closed;
        /// <summary>The version the instance follows.</summary>
        public string Version => Names[_follows];
        /// <summary>The version in force.</summary>
        public string VersionInForce => Names[_vin];
        /// <summary>Whether the contract is in force (by the bindings of the version in force).</summary>
        public bool InForce => !_out;
        /// <summary>Whether the instance has started (the contract was in force at some time).</summary>
        public bool Started => _body != null;
        public IContractInstance Body => _body;

        /// <summary>
        /// The instance follows the current regime of <paramref name="regimes"/>
        /// (register it with the runtime so that CadlRegimes.Set reaches it), or
        /// stays in the initial regime of <paramref name="version"/> when it is null.
        /// </summary>
        public {cls}Contract(string instanceId, ContractRuntime rt, long nowMs = 0,
                {pad}string version = null, CadlRegimes regimes = null) {{
            InstanceId = instanceId;
            _rt = rt;
            _vin = _current = _follows = version == null ? 0 : Index(version);
            _regime = regimes != null ? regimes.Current : CadlRegimes.InitialOf(_vin);
            if (regimes != null) _seen.AddRange(regimes.Seen); else _seen.Add(_regime);
            string why = Refusal(_vin);
            if (why != null) throw new ArgumentException(why);
            _used.Add(_vin);
            _out = !CadlRegimes.InForceIn(_vin, _regime, ContractId);
            _outSince = nowMs;
            if (!_out) _body = Create(_vin, instanceId, rt, nowMs);
        }}

        private static int Index(string name) {{
            int k = Array.IndexOf(Names, name);
            if (k < 0) throw new ArgumentException("not a version of the run: " + name);
            return k;
        }}

{create_take}        private string State => ((IVersionBody)_body).StateName;

        private void SwitchTo(int v, ContractRuntime rt, long tMs) {{
            string from = State;
            _body = Take(v, InstanceId, (IVersionBody)_body);
            _follows = v;
            _pending = -1;
            rt.Emit(new ContractEvent {{
                Kind = ContractEventKind.Lifecycle, ContractId = ContractId,
                InstanceId = InstanceId, TransitionId = "<version>", FromState = from,
                ToState = State, TMs = tMs, Cause = "version " + Names[v],
            }});
        }}

        private void CheckPending(ContractRuntime rt, long tMs) {{
            if (_pending >= 0 && !Closed && Array.IndexOf(States[_pending], State) >= 0)
                SwitchTo(_pending, rt, tMs);
        }}

        // version k, which took effect at tVersion, reaches the instance at tMs
        // (versioned_class.SetVersion): a switch that it makes is reported at
        // tVersion (K.11, "Versions and regimes", item 4); a waiting switch
        // with in_states: is reported when it happens
        private void Step(int k, long tMs, long tVersion) {{
            if (Closed) {{ _current = k; return; }}
            CheckPending(_rt, tMs);
            for (int v = _current + 1; v <= k; v++) {{
                _pending = -1;
                if (Rules[v] == "keep") continue;
                if (States[v].Length == 0 || Array.IndexOf(States[v], State) >= 0) SwitchTo(v, _rt, tVersion);
                else _pending = v;
            }}
            _current = k;
        }}

        // why version v cannot be in force with the regimes current so far (K.11), or null
        private string Refusal(int v) {{
            foreach (var r in _seen) {{
                if (!CadlRegimes.HasRegime(v, r))
                    return "the run is in regime " + (r ?? "(none: a version without regimes:)")
                        + ", which is not a regime of version " + Names[v]
                        + "; a regime of a run with versions must be a regime of every version that the run uses (Appendix K, K.11)";
            }}
            return null;
        }}

        /// <summary>Why the run cannot be in regime r (K.11), or null; called by CadlRegimes.Set.</summary>
        public string RefuseRegime(string r) {{
            if (Closed) return null;
            foreach (var v in _used) {{
                if (!CadlRegimes.HasRegime(v, r))
                    return "the run is in regime " + r + ", which is not a regime of version " + Names[v]
                        + "; a regime of a run with versions must be a regime of every version that the run uses (Appendix K, K.11)";
            }}
            return null;
        }}

        /// <summary>Called by CadlRegimes.Set at time tMs.</summary>
        public void SetRegime(string r, long tMs) {{
            _regime = r;
            if (!_seen.Contains(r)) _seen.Add(r);
            if (!Closed) Follow(tMs);
        }}

        // whether the contract is in force now, and what follows from it
        private void Follow(long tMs) {{
            bool o = !CadlRegimes.InForceIn(_vin, _regime, ContractId);
            if (o == _out) return;
            if (o) {{ _out = true; _outSince = tMs; return; }}
            _out = false;
            if (_body == null) {{
                // the instance starts, under the version in force
                _current = _follows = _vin;
                _waiting.Clear();
                _waitingT.Clear();
                _body = Create(_vin, InstanceId, _rt, tMs);
                return;
            }}
            long d = tMs - _outSince;
            if (d > 0) ((IVersionBody)_body).Shift(d);
            var w = new List<int>(_waiting);
            var wt = new List<long>(_waitingT);
            _waiting.Clear();
            _waitingT.Clear();
            for (int i = 0; i < w.Count; i++) Step(w[i], tMs, wt[i]);
        }}

        /// <summary>The version is in force from tMs on (K.11).</summary>
        public void SetVersion(string version, long tMs, ContractRuntime rt) {{
            int k = Index(version);
            // after the end of the instance only the name is checked, as cadl norms-eval
            if (Closed) {{ _vin = _current = k; return; }}
            if (k < _vin)
                throw new ArgumentException("version " + version + " comes before the version in force");
            string why = Refusal(k);
            if (why != null) throw new ArgumentException(why);
            _vin = k;
            if (!_used.Contains(k)) _used.Add(k);
            if (_body == null) {{ Follow(tMs); return; }}
            bool wasOut = _out;
            if (wasOut || !CadlRegimes.InForceIn(k, _regime, ContractId)) {{
                _waiting.Add(k);
                _waitingT.Add(tMs);
                Follow(tMs);
                return;
            }}
            Step(k, tMs, tMs);
        }}

        public void HandleEvent(in PortEvent ev, ContractRuntime rt) {{
            if (_body == null || _out) return;
            CheckPending(rt, ev.TMs);
            _body.HandleEvent(ev, rt);
        }}

        public void Tick(long nowMs, ContractRuntime rt) {{
            if (_body == null || _out) return;
            CheckPending(rt, nowMs);
            _body.Tick(nowMs, rt);
        }}

        public IDictionary<string, object> WorldSnapshot() =>
            _body != null ? _body.WorldSnapshot() : new Dictionary<string, object>();
    }}
}}
"""
    return head + cls_text


def regimes_versions_file(chain, contract_ids: list, namespace: str = "CADL.SosDsl") -> str:
    """Generated/CadlRegimes.cs of a run with versions of which some bind
    regimes (K.11): the regimes of each version, the contracts each does not
    have in force, and the current regime that the host sets."""
    from ...regimes import checked, initial_regime
    names, regs, inits, rows, everyone = [], [], [], [], []
    for info in chain.infos:
        names.append(info.name)
        bindings = checked(info.sos) if info.regimes is not None else None
        if not bindings:
            regs.append("null")
            inits.append("null")
            rows.append("null")
            continue
        regs.append("new string[] { " + ", ".join(_csharp_string(b.name) for b in bindings) + " }")
        inits.append(_csharp_string(initial_regime(info.sos)))
        rows.append("new Dictionary<string, HashSet<string>> { " + " ".join(
            f"{{ {_csharp_string(b.name)}, new HashSet<string> {{ "
            + ", ".join(_csharp_string(c) for c in contract_ids if c not in b.active) + " } },"
            for b in bindings) + " }")
        everyone += [b.name for b in bindings if b.name not in everyone]
    table = ",\n            ".join(rows)
    return f"""// <auto-generated>
//   Regimes of the versions of a run (regimes:, Appendix A, A.7.1; Appendix K, K.11)
// </auto-generated>
using System;
using System.Collections.Generic;

namespace {namespace} {{

    /// <summary>An instance that follows the current regime.</summary>
    public interface IRegimeBound {{
        string RefuseRegime(string regime);
        void SetRegime(string regime, long tMs);
    }}

    /// <summary>
    /// The current regime of a run with versions (versions {", ".join(names)}).
    /// The host calls Set(regime, tMs) when the regime changes (the regime
    /// controller of the run decides it, A.7.1); before that the run is in the
    /// initial regime of the version in force at its start (the constructor
    /// takes it; the first version by default), or in no regime when that
    /// version has no regimes:. Whether a contract is in force depends on the
    /// regime bindings of the version in force for its instance (K.11).
    /// </summary>
    public sealed class CadlRegimes {{
        private static readonly string[] Versions = {{ {", ".join(_csharp_string(x) for x in names)} }};
        private static readonly string[][] RegimesOf = {{ {", ".join(regs)} }};
        private static readonly string[] Initials = {{ {", ".join(inits)} }};
        private static readonly Dictionary<string, HashSet<string>>[] NotInForce = {{
            {table}
        }};
        private static readonly HashSet<string> All = new HashSet<string> {{ {", ".join(_csharp_string(x) for x in everyone)} }};

        private readonly ContractRuntime _rt;
        private readonly List<string> _seen = new List<string>();

        /// <summary>The current regime (null: the run is in no regime).</summary>
        public string Current {{ get; private set; }}
        /// <summary>The regimes current at some time so far.</summary>
        public IReadOnlyList<string> Seen => _seen;

        public CadlRegimes(ContractRuntime rt, string version = null) {{
            _rt = rt ?? throw new ArgumentNullException(nameof(rt));
            int k = version == null ? 0 : Array.IndexOf(Versions, version);
            if (k < 0) throw new ArgumentException("not a version of the run: " + version);
            Current = Initials[k];
            _seen.Add(Current);
        }}

        public static string InitialOf(int v) => Initials[v];

        /// <summary>Whether the run may be in regime r (null: in no regime) under version v.</summary>
        public static bool HasRegime(int v, string r) =>
            r == null ? RegimesOf[v] == null : RegimesOf[v] != null && Array.IndexOf(RegimesOf[v], r) >= 0;

        public static bool InForceIn(int v, string regime, string contractId) =>
            NotInForce[v] == null || regime == null || !NotInForce[v][regime].Contains(contractId);

        /// <summary>The regime is current from time tMs on.</summary>
        public void Set(string regime, long tMs) {{
            if (regime == null || !All.Contains(regime))
                throw new ArgumentException("not a regime of a version of the run: " + regime);
            foreach (var inst in _rt.Instances) {{
                if (inst is IRegimeBound b) {{
                    string why = b.RefuseRegime(regime);
                    if (why != null) throw new ArgumentException(why);
                }}
            }}
            Current = regime;
            if (!_seen.Contains(regime)) _seen.Add(regime);
            foreach (var inst in _rt.Instances) {{
                if (inst is IRegimeBound b) b.SetRegime(regime, tMs);
            }}
        }}
    }}
}}
"""


def version_body_interface(namespace: str = "CADL.SosDsl", regimes: bool = False) -> str:
    """Generated/CadlVersions.cs: what a versioned class reads of a body."""
    shift = "        void Shift(long d);\n" if regimes else ""
    return f"""// <auto-generated>
//   Versions of a run (Appendix K, K.11)
// </auto-generated>
using System.Collections.Generic;

namespace {namespace} {{
    /// <summary>What the versioned class of a contract reads of the body of a version.</summary>
    public interface IVersionBody {{
        string StateName {{ get; }}
        long ArmedAt {{ get; }}
        Dictionary<string, long> MonitorLast();
{shift}    }}
}}
"""


def regimes_file(regimes: dict, namespace: str = "CADL.SosDsl") -> str:
    """Generated/CadlRegimes.cs: the regimes of the file, the contracts each
    does not have in force, and the current regime that the host sets."""
    rows = "\n".join(
        f"            {{ {_csharp_string(r)}, new HashSet<string> {{ "
        + ", ".join(_csharp_string(c) for c in regimes["out"][r]) + " } },"
        for r in regimes["regimes"])
    return f"""// <auto-generated>
//   Regimes of the file (regimes:, Appendix A, A.7.1)
// </auto-generated>
using System;
using System.Collections.Generic;

namespace {namespace} {{

    /// <summary>An instance that follows the current regime.</summary>
    public interface IRegimeBound {{
        void SetInForce(bool inForce, long tMs);
    }}

    /// <summary>
    /// The current regime of a run. The host calls Set(regime, tMs) when the
    /// regime changes (the regime controller of the run decides it, A.7.1);
    /// before that the run is in the initial regime. An instance of a
    /// contract that the current regime does not have in force sees no
    /// event and no tick, and its clock stops: when the contract is in
    /// force again, its deadlines and periodic monitors are moved on by
    /// the time it was not in force (F.5, "Contracts not in force").
    /// </summary>
    public sealed class CadlRegimes {{
        public static readonly string Initial = {_csharp_string(regimes["initial"])};

        private static readonly Dictionary<string, HashSet<string>> NotInForce =
            new Dictionary<string, HashSet<string>> {{
{rows}
        }};

        private readonly ContractRuntime _rt;

        public string Current {{ get; private set; }} = Initial;

        public CadlRegimes(ContractRuntime rt) {{
            _rt = rt ?? throw new ArgumentNullException(nameof(rt));
        }}

        public static bool InForceIn(string regime, string contractId) {{
            return !NotInForce[regime].Contains(contractId);
        }}

        public bool InForce(string contractId) => InForceIn(Current, contractId);

        /// <summary>The regime is current from time tMs on.</summary>
        public void Set(string regime, long tMs) {{
            if (regime == null || !NotInForce.ContainsKey(regime))
                throw new ArgumentException("not a regime of the file: " + regime);
            Current = regime;
            foreach (var inst in _rt.Instances) {{
                if (inst is IRegimeBound b) b.SetInForce(InForce(inst.ContractId), tMs);
            }}
        }}
    }}
}}
"""


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------

def _emit_state_enum(contract: ContractDef, cls: str, ns: str) -> str:
    lc = contract.lifecycle or LifecycleSpec()
    states = list(lc.states) or ["Initial"]
    members = ",\n        ".join(_csharp_member(s) for s in states)
    return (
        "// <auto-generated>\n"
        f"//   contract id : {contract.id}\n"
        f"//   states      : {len(states)}\n"
        f"//   transitions : {len(lc.transitions)}\n"
        "// </auto-generated>\n"
        f"namespace {ns} {{\n"
        f"    public enum {cls}State {{\n"
        f"        {members},\n"
        f"    }}\n"
        f"}}\n"
    )


# ---------------------------------------------------------------------------
# Contract class
# ---------------------------------------------------------------------------

def _emit_contract_class(contract: ContractDef, cls: str, ns: str) -> str:
    lc = contract.lifecycle or LifecycleSpec()
    initial = lc.initial or (lc.states[0] if lc.states else "Initial")
    initial_member = _csharp_member(initial)
    # Declaration order with duplicates removed. A set here made the order of
    # the emitted `case` lines vary from run to run (string hashing is
    # randomised per process), so regenerating produced spurious diffs.
    terminal_states = list(dict.fromkeys(lc.terminal))

    is_terminal_body = ",\n            ".join(
        f"{cls}State.{_csharp_member(s)}" for s in terminal_states
    ) or "/* none */"

    # Build a transition table: per-(from-state) list of edges sorted
    # so deadline-bearing transitions come last (informative only).
    transition_cases: list[str] = []
    for tr in lc.transitions:
        ev_lit = _csharp_string(tr.on or "")
        when_block = ""
        if tr.when:
            # Guard failed → skip this transition. Each transition is an
            # independent `if (...) { ...; FireTransition(...); return; }`
            # block (no enclosing loop/switch), so the correct skip is
            # `return;`. Emitting `break;` here produces invalid C#
            # (CS0139: no enclosing loop out of which to break).
            when_block = (
                f"                if (!PredicateEvaluator.Evaluate("
                f"{_csharp_string(tr.when)}, snap)) return;\n"
            )
        from_match = " || ".join(
            f"_state == {cls}State.{_csharp_member(fs)}"
            for fs in tr.from_states
        )
        transition_cases.append(
            f"            // transition: {tr.id}\n"
            f"            if (ev.Name == {ev_lit} && ({from_match})) {{\n"
            f"                var snap = rt.SnapshotFor(this);\n"
            f"{when_block}"
            f"                FireTransition(rt, ev.TMs,\n"
            f"                    transitionId: {_csharp_string(tr.id)},\n"
            f"                    toState: {cls}State.{_csharp_member(tr.to_state)},\n"
            f"                    cause: \"event\");\n"
            f"                return;\n"
            f"            }}\n"
        )

    # Emit deadline arming: when entering a state, look at all
    # outgoing transitions of that state with deadline_ms; arm a
    # timer for each.
    arm_cases: list[str] = []
    for tr in lc.transitions:
        if tr.deadline_ms is None or not tr.on_violation:
            continue
        target_state = (
            tr.on_violation.transition
            if tr.on_violation and tr.on_violation.transition
            else None
        )
        if not target_state:
            continue
        sev = (tr.on_violation.severity if tr.on_violation else "Major") or "Major"
        for fs in tr.from_states:
            arm_cases.append(
                f"            if (newState == {cls}State.{_csharp_member(fs)}) {{\n"
                f"                _deadlines.Add(new Deadline(\n"
                f"                    transitionId: {_csharp_string(tr.id)},\n"
                f"                    deadlineMs: nowMs + {int(tr.deadline_ms)},\n"
                f"                    targetState: {cls}State.{_csharp_member(target_state)},\n"
                f"                    severity: Severity.{sev}));\n"
                f"            }}\n"
            )

    is_terminal_method = (
        "        private bool IsTerminal(" + cls + "State s) {\n"
        "            switch (s) {\n"
        + ''.join(
            f"                case {cls}State.{_csharp_member(s)}: return true;\n"
            for s in terminal_states
        )
        + "                default: return false;\n"
        "            }\n"
        "        }\n"
    )

    # Pre-compute strings that must contain backslash escapes — they
    # cannot live inside f-string expression parts (Python 3.10).
    arm_block = ''.join(arm_cases) if arm_cases else (
        "            // no deadlines declared\n"
    )
    transitions_block = ''.join(transition_cases)

    # Optional reference to monitor evaluator (only emit if the
    # contract has at least one monitor).
    has_monitors = bool(contract.monitors)
    monitor_field = (
        f"        private readonly {cls}Monitors _monitors;\n"
        if has_monitors else ""
    )
    monitor_init = (
        f"            _monitors = new {cls}Monitors(this);\n"
        if has_monitors else ""
    )
    monitor_event = (
        "            _monitors?.OnEvent(ev, rt);\n"
        if has_monitors else ""
    )
    monitor_tick = (
        "            _monitors?.Tick(nowMs, rt);\n"
        if has_monitors else ""
    )

    return f"""// <auto-generated>
//   Source: CADL contract {contract.id}
// </auto-generated>
using System;
using System.Collections.Generic;

namespace {ns} {{

    /// <summary>
    /// Per-instance contract execution for {contract.id}. Drive it
    /// by registering it on a ContractRuntime, then forwarding port
    /// messages via rt.PostEvent and sim ticks via rt.Tick.
    /// </summary>
    public sealed class {cls}Contract : IContractInstance {{

        public string ContractId => {_csharp_string(contract.id)};
        public string InstanceId {{ get; }}
        public {cls}State State => _state;
        public bool Closed {{ get; private set; }}

        private {cls}State _state;
        private readonly List<Deadline> _deadlines = new List<Deadline>();
        private readonly Dictionary<string, object> _instanceWorld =
            new Dictionary<string, object>();
{monitor_field}
        public {cls}Contract(string instanceId, ContractRuntime rt, long nowMs = 0) {{
            if (string.IsNullOrEmpty(instanceId))
                throw new ArgumentException("instanceId must be non-empty");
            InstanceId = instanceId;
            _state = {cls}State.{initial_member};
{monitor_init}
            rt.Emit(new ContractEvent {{
                Kind = ContractEventKind.Lifecycle,
                ContractId = ContractId,
                InstanceId = InstanceId,
                TransitionId = "<open>",
                FromState = string.Empty,
                ToState = _state.ToString(),
                TMs = nowMs,
                Cause = "open",
            }});

            ArmDeadlinesFor(_state, nowMs);
        }}

        public IDictionary<string, object> WorldSnapshot() {{
            // The runtime composes _world (global) + this dict + state.
            var d = new Dictionary<string, object>(_instanceWorld);
            d["state"] = _state.ToString();
            return d;
        }}

        public void SetInstanceField(string key, object value) {{
            if (!string.IsNullOrEmpty(key)) _instanceWorld[key] = value;
        }}

        public void HandleEvent(in PortEvent ev, ContractRuntime rt) {{
            if (Closed) return;
{transitions_block}
{monitor_event}
        }}

        public void Tick(long nowMs, ContractRuntime rt) {{
            if (Closed) return;
            // 1) Expire deadlines
            for (int i = _deadlines.Count - 1; i >= 0; i--) {{
                var d = _deadlines[i];
                if (d.DeadlineMs <= nowMs) {{
                    _deadlines.RemoveAt(i);
                    rt.Emit(new ContractEvent {{
                        Kind = ContractEventKind.Violation,
                        ContractId = ContractId,
                        InstanceId = InstanceId,
                        Norm = d.TransitionId,
                        Severity = d.Severity,
                        TMs = nowMs,
                        DetectedBy = "deadline:" + d.TransitionId,
                    }});
                    JumpToState(rt, d.TargetState, nowMs, "deadline");
                    if (Closed) return;
                }}
            }}
            // 2) Periodic monitors
{monitor_tick}
        }}

        // --- helpers ---

        public void FireTransition(ContractRuntime rt, long tMs,
                                   string transitionId,
                                   {cls}State toState,
                                   string cause) {{
            var oldState = _state;
            _state = toState;

            // cancel any deadline tied to this transition
            for (int i = _deadlines.Count - 1; i >= 0; i--) {{
                if (_deadlines[i].TransitionId == transitionId) _deadlines.RemoveAt(i);
            }}

            rt.Emit(new ContractEvent {{
                Kind = ContractEventKind.Lifecycle,
                ContractId = ContractId,
                InstanceId = InstanceId,
                TransitionId = transitionId,
                FromState = oldState.ToString(),
                ToState = toState.ToString(),
                TMs = tMs,
                Cause = cause,
            }});

            if (IsTerminal(toState)) {{
                Closed = true;
                _deadlines.Clear();
            }} else {{
                ArmDeadlinesFor(toState, tMs);
            }}
        }}

        public void JumpToState(ContractRuntime rt, {cls}State toState,
                                long tMs, string cause) {{
            var oldState = _state;
            _state = toState;
            _deadlines.Clear();
            rt.Emit(new ContractEvent {{
                Kind = ContractEventKind.Lifecycle,
                ContractId = ContractId,
                InstanceId = InstanceId,
                TransitionId = "<jump>",
                FromState = oldState.ToString(),
                ToState = toState.ToString(),
                TMs = tMs,
                Cause = cause,
            }});
            if (IsTerminal(toState)) {{
                Closed = true;
            }} else {{
                ArmDeadlinesFor(toState, tMs);
            }}
        }}

        private void ArmDeadlinesFor({cls}State newState, long nowMs) {{
{arm_block}        }}

{is_terminal_method}

        private struct Deadline {{
            public string TransitionId;
            public long DeadlineMs;
            public {cls}State TargetState;
            public Severity Severity;
            public Deadline(string transitionId, long deadlineMs,
                            {cls}State targetState, Severity severity) {{
                TransitionId = transitionId;
                DeadlineMs = deadlineMs;
                TargetState = targetState;
                Severity = severity;
            }}
        }}
    }}
}}
"""


# ---------------------------------------------------------------------------
# Monitors
# ---------------------------------------------------------------------------

def _emit_monitors(contract: ContractDef, cls: str, ns: str) -> str:
    monitors: list[MonitorDef] = list(contract.monitors)
    if not monitors:
        return (
            "// <auto-generated />\n"
            f"namespace {ns} {{\n"
            f"    /// <summary>No monitors declared for {contract.id}.</summary>\n"
            f"    public sealed class {cls}Monitors {{\n"
            f"        public {cls}Monitors({cls}Contract _) {{ }}\n"
            f"        public void OnEvent(in PortEvent ev, ContractRuntime rt) {{ }}\n"
            f"        public void Tick(long nowMs, ContractRuntime rt) {{ }}\n"
            f"    }}\n"
            f"}}\n"
        )

    eval_methods: list[str] = []
    tick_cases: list[str] = []
    event_cases: list[str] = []

    for m in monitors:
        method = _csharp_member("Eval_" + m.id)
        rule_lit = _csharp_string(m.rule or "")
        target_id = m.on_match.transition if m.on_match else None
        violation_id = (
            (m.on_match.violation if m.on_match else None) or m.id
        )
        sev = (m.on_match.severity if m.on_match else "Major") or "Major"

        # Body: evaluate predicate against snapshot; on match emit a
        # violation, then either fire a transition by id or jump to a
        # state by name (if target is a state) or do nothing.
        target_handling = ""
        if target_id:
            target_handling = (
                f"                // target: {target_id}\n"
                f"                FireTargetForMonitor(rt, nowMs, "
                f"{_csharp_string(target_id)});\n"
            )

        eval_methods.append(
            f"        private void {method}(long nowMs, ContractRuntime rt) {{\n"
            # Another monitor of the same tick or event may have moved the
            # instance to a terminal state: a closed instance is finished.
            f"            if (_owner.Closed) return;\n"
            f"            var snap = rt.SnapshotFor(_owner);\n"
            f"            if (!PredicateEvaluator.Evaluate({rule_lit}, snap)) return;\n"
            f"            rt.Emit(new ContractEvent {{\n"
            f"                Kind = ContractEventKind.Violation,\n"
            f"                ContractId = _owner.ContractId,\n"
            f"                InstanceId = _owner.InstanceId,\n"
            f"                Norm = {_csharp_string(violation_id)},\n"
            f"                Severity = Severity.{sev},\n"
            f"                TMs = nowMs,\n"
            f"                DetectedBy = \"monitor:{m.id}\",\n"
            f"            }});\n"
            f"{target_handling}"
            f"        }}\n"
        )

        if (m.sampling and m.sampling.kind == "periodic"
                and m.sampling.period_ms):
            period = int(m.sampling.period_ms)
            tick_cases.append(
                f"            // monitor: {m.id} (periodic {period}ms)\n"
                f"            if (nowMs - _last_{_csharp_member(m.id)} >= {period}) {{\n"
                f"                _last_{_csharp_member(m.id)} = nowMs;\n"
                f"                {method}(nowMs, rt);\n"
                f"            }}\n"
            )
        else:
            event_cases.append(
                f"            // monitor: {m.id} (event)\n"
                f"            {method}(ev.TMs, rt);\n"
            )

    last_fields = "\n".join(
        f"        private long _last_{_csharp_member(m.id)} = long.MinValue / 2;"
        for m in monitors
        if m.sampling and m.sampling.kind == "periodic"
    )
    # Pre-compute defaults containing backslashes (no f-string traps).
    event_block = ''.join(event_cases) if event_cases else (
        "            // no event-driven monitors\n"
    )
    tick_block = ''.join(tick_cases) if tick_cases else (
        "            // no periodic monitors\n"
    )
    eval_block = ''.join(eval_methods)

    return f"""// <auto-generated>
//   Source: CADL contract {contract.id}
//   Monitors: {len(monitors)}
// </auto-generated>
using System.Collections.Generic;

namespace {ns} {{

    /// <summary>
    /// Declarative monitors for {contract.id}. Owned by the
    /// {cls}Contract instance that constructs it; do not instantiate
    /// manually.
    /// </summary>
    public sealed class {cls}Monitors {{
        private readonly {cls}Contract _owner;
{last_fields}

        public {cls}Monitors({cls}Contract owner) {{ _owner = owner; }}

        public void OnEvent(in PortEvent ev, ContractRuntime rt) {{
{event_block}        }}

        public void Tick(long nowMs, ContractRuntime rt) {{
{tick_block}        }}

        // Fire a target transition (by id) or a state jump if the
        // target name matches a state in {cls}State.
        private void FireTargetForMonitor(ContractRuntime rt, long nowMs,
                                          string targetId) {{
            // Try state-name jump first (most common: 'Violated').
            if (System.Enum.TryParse<{cls}State>(targetId, out var target)) {{
                _owner.JumpToState(rt, target, nowMs, "monitor");
                return;
            }}
            // Otherwise interpret as a transition id; the contract
            // class knows how to find and run it.
            // (For v0.1 we only emit the jump form, which covers all
            // examples in Appendix E.)
        }}

{eval_block}    }}
}}
"""
