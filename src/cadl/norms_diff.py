"""Static comparison of two versions of a file for an amendment
(Appendix F, F.6, norms 0.1).

`diff_versions` lists, for each contract with norms that both versions
have, what an amendment from the old version to the new one does to norm
instances: norms removed, added or of a changed kind, every norm that
carries over with a changed ``content`` or ``deadline:`` (the report F.6
asks for before a new version takes effect), ``transform:`` targets that
the new version lacks, and changed ``counts_as:`` rules.

Each finding has a level: ``error`` (the amendment cannot do what the old
version asks), ``warning`` (running instances are affected) or ``info``.
"""

from __future__ import annotations

from .ast_nodes import ContractDef, NormDef, SoSDefinition
from .norms_eval import NormsEvalError, _fmt_d, _targets_of

_FIELDS = ("bearer", "counterparty", "trigger", "until", "on_violation", "effects",
           "on_amendment", "transform_to")


def _finding(level: str, kind: str, text: str, **more) -> dict:
    return {"level": level, "kind": kind, **more, "text": text}


def _policy(d: NormDef) -> str:
    return f"transform: {d.transform_to}" if d.on_amendment == "transform" else d.on_amendment


def _diff_contract(old: ContractDef, new: ContractDef) -> list[dict]:
    out: list[dict] = []
    old_norms = {d.id: d for d in old.norms}
    new_norms = {d.id: d for d in new.norms}
    new_targets = _targets_of(new)

    for nid, o in old_norms.items():
        n = new_norms.get(nid)
        policy = o.on_amendment
        if policy == "transform":
            target = o.transform_to
            if target not in new_norms:
                out.append(_finding(
                    "error", "transform_target_missing",
                    f"{nid}: on_amendment is transform: {target}, but the new version has "
                    f"no norm '{target}'; an active instance would be discharged with "
                    f"nothing in its place", norm=nid, target=target))
            else:
                out.append(_finding(
                    "info", "transform",
                    f"{nid}: an active instance is discharged and {target} "
                    f"({new_norms[target].kind}, deadline {_fmt_d(new_norms[target].deadline_ms)}) "
                    f"is activated with the time already elapsed", norm=nid, target=target))
        if n is None:
            level = "warning" if policy == "carry_over" else "info"
            tail = ("an active instance is discharged, an inactive one removed"
                    if policy == "carry_over" else
                    f"an active instance follows on_amendment ({_policy(o)}), an inactive one "
                    f"is removed")
            out.append(_finding(level, "norm_removed", f"{nid}: removed; {tail}", norm=nid))
            continue
        if o.kind != n.kind:
            tail = ("an active instance is discharged" if policy == "carry_over" else
                    f"an active instance follows on_amendment ({_policy(o)})")
            out.append(_finding(
                "warning", "kind_changed",
                f"{nid}: kind changes from {o.kind} to {n.kind}; {tail}",
                norm=nid, old=o.kind, new=n.kind))
        changed = {}
        if o.content.strip() != n.content.strip():
            changed["content"] = {"old": o.content, "new": n.content}
        if o.deadline_ms != n.deadline_ms:
            changed["deadline"] = {"old": _fmt_d(o.deadline_ms), "new": _fmt_d(n.deadline_ms)}
        if changed and policy == "carry_over" and o.kind == n.kind:
            parts = [f"{k} changes from '{v['old']}' to '{v['new']}'" for k, v in changed.items()]
            tail = ""
            if ("deadline" in changed and n.deadline_ms is not None and n.kind == "obligation"
                    and (o.deadline_ms is None or n.deadline_ms < o.deadline_ms)):
                tail = ("; an active instance whose elapsed time exceeds the new deadline is "
                        "violated when the new version takes effect")
            out.append(_finding(
                "warning", "carry_over_changed",
                f"{nid}: carries over, and {'; '.join(parts)}{tail}", norm=nid, changes=changed))
        elif changed:
            out.append(_finding(
                "info", "norm_changed",
                f"{nid}: {' and '.join(changed)} change(s); an active instance does not "
                f"carry over (on_amendment: {_policy(o)})"
                if o.kind == n.kind else f"{nid}: {' and '.join(changed)} change(s)",
                norm=nid, changes=changed))
        other = [f for f in _FIELDS if getattr(o, f) != getattr(n, f)]
        if other:
            out.append(_finding(
                "info", "norm_changed", f"{nid}: {', '.join(other)} change(s)",
                norm=nid, fields=other))
    for nid, n in new_norms.items():
        if nid not in old_norms:
            at_once = n.trigger is None and nid not in new_targets
            out.append(_finding(
                "info", "norm_added",
                f"{nid}: added ({n.kind}); " + ("it has no trigger, so it is active at once"
                                                if at_once else "it starts inactive"),
                norm=nid, active_at_once=at_once))

    def rules(c: ContractDef) -> dict:
        by: dict[str, list] = {}
        for r in c.counts_as:
            by.setdefault(r.creates, []).append(
                r.when.strip() + (f" if {r.if_.strip()}" if r.if_ else ""))
        return {k: sorted(v) for k, v in by.items()}

    old_rules, new_rules = rules(old), rules(new)
    for name in list(old_rules) + [k for k in new_rules if k not in old_rules]:
        a, b = old_rules.get(name), new_rules.get(name)
        if a == b:
            continue
        if b is None:
            out.append(_finding("info", "counts_as_removed",
                                f"counts_as: {name} is no longer created (was: {'; '.join(a)})",
                                event=name, old=a))
        elif a is None:
            out.append(_finding("info", "counts_as_added",
                                f"counts_as: {name} is new (when: {'; '.join(b)})",
                                event=name, new=b))
        else:
            out.append(_finding("info", "counts_as_changed",
                                f"counts_as: {name} changes from '{'; '.join(a)}' to "
                                f"'{'; '.join(b)}'", event=name, old=a, new=b))

    if old.lifecycle is not None:
        kept = set(new.lifecycle.states) if new.lifecycle is not None else set()
        gone = [s for s in old.lifecycle.states if s not in kept]
        if gone and new.lifecycle is not None:
            out.append(_finding(
                "warning", "lifecycle_states_removed",
                f"lifecycle: the state(s) {', '.join(gone)} are removed; an instance in one "
                f"of them cannot continue under the new version", states=gone))
    return out


def diff_versions(old: SoSDefinition, new: SoSDefinition,
                  contract_id: str | None = None) -> dict:
    """Compare the contracts with norms of two versions. Returns a
    JSON-serializable dict with ``contracts[]`` (``id``, ``findings[]``),
    ``errors`` (the texts of the error-level findings) and ``ok``."""
    new_by = {c.id: c for c in new.contracts}
    if contract_id is not None:
        pool = [c for c in old.contracts if c.id == contract_id]
        if not pool:
            raise NormsEvalError(f"no contract '{contract_id}' in the old version")
    else:
        pool = [c for c in old.contracts
                if c.norms or c.counts_as
                or (c.id in new_by and (new_by[c.id].norms or new_by[c.id].counts_as))]
    contracts = []
    for c in pool:
        if c.id not in new_by:
            findings = [_finding(
                "error" if contract_id is not None else "warning", "contract_removed",
                f"the new version has no contract '{c.id}'; its instances cannot be amended")]
        else:
            findings = _diff_contract(c, new_by[c.id])
        contracts.append({"id": c.id, "findings": findings})
    errors = [f"{c['id']}: {f['text']}" for c in contracts for f in c["findings"]
              if f["level"] == "error"]
    return {"contracts": contracts, "errors": errors, "ok": not errors}


def format_diff_text(result: dict) -> str:
    lines = [f"Amendment from {result.get('old', 'old')} to {result.get('new', 'new')} "
             f"(norms 0.1, F.6)"]
    for e in result.get("invalid", []):
        lines += ["", f"  error    {e}"]
    for c in result["contracts"]:
        lines += ["", f"Contract {c['id']}"]
        if not c["findings"]:
            lines.append("  no difference in norms: or counts_as:")
        for f in c["findings"]:
            lines.append(f"  {f['level']:<8} {f['text']}")
    if not result["contracts"] and not result.get("invalid"):
        lines += ["", "No contract with norms: in both versions"]
    n = len(result["errors"])
    lines += ["", "Result: nothing prevents the amendment" if result["ok"]
              else f"Result: {n} error(s) prevent the amendment"]
    return "\n".join(lines)
