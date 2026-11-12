"""Independent model of G.4 to G.7 and F.3, written from the text of the
appendices by the reviewer of the runtime (second version), and adapted
to the one rule that the second review added: an occurrence that depends
on an unbound index is unknown and resolved fail-closed (G.4). It shares
no code with `cadl`: it reads the YAML directly. It supports host-created
instances with bindings, norms with a message content and message
`trigger:` / `until:`, delegations, holds, the release decided again and
governed observations. Not modelled: deadlines of norms, powers,
lifecycles, amendments, predicates.

Not a test module: tests/test_agents_runtime.py compares the runtime with
it on generated traces."""

from __future__ import annotations

import re
from fractions import Fraction

import yaml
UNIT = {"ms": Fraction(1, 1000), "s": 1, "min": 60, "h": 3600}
def dur(x):
    m = re.fullmatch(r"(\d+)\s*(ms|s|min|h)", str(x)); return int(m[1]) * UNIT[m[2]]
def pref(s):
    s = s.replace(" ", ""); m = re.fullmatch(r"(\w+)(?:\[(.*)\])?", s); return (m[1], m[2])
def pev(s):
    a, rest = s.split("->"); b, n = rest.split(":"); return pref(a), pref(b), n.strip().split("(")[0]
def AND(*v): return False if False in v else (None if None in v else True)
class Model:
    def __init__(self, text, host=()):
        d = yaml.safe_load(text)["sos"]
        self.actors = {}
        for a in d["actors"]:
            name, idx = pref(a["id"]); rng = None
            if idx:
                lo, hi = idx.split(".."); rng = (int(lo), int(hi) if hi.isdigit() else None)
            ag = a.get("agent") or {}
            self.actors[name] = dict(rng=rng, kind=ag.get("kind"), tools=ag.get("tools", []))
        self.cs = []
        for c in d["contracts"]:
            if "enforcement" not in c: continue
            enf = c["enforcement"] or {}
            C = dict(id=c["id"], parties=[pref(p) for p in c["parties"]], default=enf.get("default"),
                     rules=[(pev(r["when"]), r["creates"]) for r in c.get("counts_as", []) if "->" in r["when"]],
                     norms=[], deleg={}, insts=[], host=c["id"] in host)
            for n in c.get("norms", []):
                if n["kind"] not in ("permission", "prohibition") or "->" not in n["content"]: continue
                od = n.get("on_denied") or {}
                C["norms"].append(dict(id=n["id"], kind=n["kind"], ev=pev(n["content"]),
                    trig=pev(n["trigger"]) if n.get("trigger") else None, until=pev(n["until"]) if n.get("until") else None,
                    esc=pref(od["escalate_to"]) if od.get("escalate_to") else None,
                    timeout=dur(od["timeout"]) if od.get("timeout") else None))
            for g in c.get("delegations", []) or []:
                C["deleg"][g["norm"]] = dict(to=[pref(x) for x in g["to"]], grant=g["grant"], revoke=g.get("revoke"),
                    depth=g.get("max_depth", 1), until=pev(g["until"]) if g.get("until") else None)
            if not C["host"]: self.create(C, "default", {})
            self.cs.append(C)
        self.holds = {}; self.nh = 0; self.out = []
    def create(self, C, name, b):
        C["insts"].append(dict(name=name, b=b, run=True, st={n["id"]: ("inactive" if n["trig"] else "active") for n in C["norms"]},
                               held={nid: {} for nid in C["deleg"]}))
    # --- membership and matching
    def member(self, name, actor):
        an, ai = actor
        if an != name or name not in self.actors: return False
        rng = self.actors[name]["rng"]
        if rng is None: return ai is None
        return ai is not None and ai.isdigit() and rng[0] <= int(ai) and (rng[1] is None or int(ai) <= rng[1])
    def match(self, ref, actor, b, unbound):
        """unbound: value of the match when the index variable has no binding"""
        name, idx = ref
        if not self.member(name, actor): return False
        if idx is None or idx == "*": return True
        if idx.isdigit(): return actor[1] == idx
        if ".." in idx:
            lo, hi = idx.split("..")
            return int(lo) <= int(actor[1]) and (not hi.isdigit() or int(actor[1]) <= int(hi))
        if idx in b: return int(actor[1]) == b[idx]
        return unbound
    def occurs(self, ev, m, b, unbound):   # G.4: `unbound` resolves an unknown occurrence
        return ev[2] == m[2] and self.match(ev[0], m[0], b, unbound) and self.match(ev[1], m[1], b, unbound)
    def applies(self, n, m, b):            # G.4/G.5: Kleene, unbound is unknown
        if n["ev"][2] != m[2]: return False
        return AND(self.match(n["ev"][0], m[0], b, None), self.match(n["ev"][1], m[1], b, None))
    def kind(self, m):
        r = self.actors.get(m[1][0])
        if not r or r["kind"] != "tool_server" or not self.member(m[1][0], m[1]): return "ordinary"
        return "tool" if m[2] in r["tools"] else "undeclared"
    def governs(self, C, m):
        party = lambda a: any(self.match(p, a, {}, True) for p in C["parties"])
        return party(m[1]) and (self.kind(m) != "ordinary" or party(m[0]))
    def run(self, C): return [i for i in C["insts"] if i["run"]]
    def valid(self, C, I, nid, holder):
        g = C["deleg"][nid]; ch = I["held"][nid].get(holder)
        return bool(ch) and I["st"][nid] == "active" and len(ch) - 1 <= g["depth"] and all(
            any(self.match(r, pref(a), {}, False) for r in g["to"]) for a in ch[1:])
    def grounds(self, C, m):
        return [(n, I) for n in C["norms"] if n["kind"] == "prohibition" for I in self.run(C)
                if I["st"][n["id"]] == "active" and self.applies(n, m, I["b"]) is not False]
    def approver(self, n, I):
        if n["esc"] is None: return None
        name, idx = n["esc"]
        if idx is None: return name
        if idx.isdigit(): return f"{name}[{idx}]"
        return f"{name}[{I['b'][idx]}]" if idx in I["b"] else None
    def contract(self, C, m, txt):
        """step 3 of G.5 for one contract: (decision, reason, approver, timeout, prov)"""
        k = self.kind(m)
        dflt = C["default"] or ("deny" if k == "tool" else "allow")
        if not self.run(C):
            return ("deny", "no_instance", None, None, []) if k == "tool" else (dflt, "default", None, None, [])
        g = self.grounds(C, m)
        if g:
            ap = {self.approver(n, I) for n, I in g}
            if None not in ap and len(ap) == 1 and next(iter(ap)) != txt[0]:
                return ("escalate", "prohibition", next(iter(ap)), min([n["timeout"] for n, _ in g if n["timeout"] is not None], default=None), [])
            return ("deny", "prohibition", None, None, [])
        ok = False; prov = []
        for n in C["norms"]:
            if n["kind"] != "permission": continue
            for I in self.run(C):
                if I["st"][n["id"]] == "active" and self.applies(n, m, I["b"]) is True: ok = True
            for I in self.run(C):
                if n["id"] in C["deleg"] and n["ev"][2] == m[2] and self.match(n["ev"][1], m[1], I["b"], None) is True \
                        and self.valid(C, I, n["id"], txt[0]):
                    ch = I["held"][n["id"]][txt[0]]
                    if self.match(n["ev"][0], pref(ch[0]), I["b"], None) is True:
                        ok = True
                        if ch not in prov: prov.append(ch)
        if ok: return ("allow", "permission", None, None, prov)
        return (dflt, "default", None, None, [])
    def decide(self, m, txt):
        gov = [C for C in self.cs if self.governs(C, m)]
        if not gov: return "allow", "ungoverned", None, None, []
        if self.kind(m) == "undeclared": return "deny", "undeclared_tool", None, None, []
        res = [self.contract(C, m, txt) for C in gov]
        den = [r for r in res if r[0] == "deny"]; esc = [r for r in res if r[0] == "escalate"]
        order = ["no_instance", "prohibition", "default"]
        if den: return "deny", min((r[1] for r in den), key=order.index), None, None, []
        if esc:
            if len({r[2] for r in esc}) > 1: return "deny", "prohibition", None, None, []
            return "escalate", "prohibition", esc[0][2], min((r[3] for r in esc if r[3] is not None), default=None), []
        prov = []
        for r in res:
            for p in r[4]:
                if p not in prov: prov.append(p)
        return "allow", ("permission" if any(r[1] == "permission" for r in res) else "default"), None, None, prov
    # --- observation (only instances of governing contracts, G.5)
    def observe(self, m, txt, args, t):
        events = {}
        for C in self.cs:
            if not self.governs(C, m): continue
            for I in self.run(C):
                for ev, creates in C["rules"]:
                    if creates in ("approval", "rejection") and self.occurs(ev, m, I["b"], False):
                        hid = args[0] if args and isinstance(args[0], str) else None
                        events.setdefault((creates, hid), set()).add((C["id"], I["name"]))
                for nid, g in C["deleg"].items():
                    n = next(x for x in C["norms"] if x["id"] == nid); x, y = txt[0], txt[1]; held = I["held"][nid]
                    if m[2] == g["grant"]:
                        ch = None
                        if self.match(n["ev"][0], m[0], I["b"], None) is True:
                            if I["st"][nid] == "active": ch = [x]
                        elif self.valid(C, I, nid, x): ch = held[x]
                        if ch and any(self.match(r, m[1], {}, False) for r in g["to"]) and y not in ch \
                                and len(ch) - 1 < g["depth"] and y not in held:
                            held[y] = ch + [y]
                    elif g["revoke"] and m[2] == g["revoke"]:
                        ch = held.get(y)
                        if ch and x in ch[:-1]:
                            for h_ in [h_ for h_, c_ in held.items() if y in c_]: del held[h_]
                for nid, g in C["deleg"].items():
                    if g["until"] and self.occurs(g["until"], m, I["b"], True): I["held"][nid].clear()
                for n in C["norms"]:
                    if n["trig"] and I["st"][n["id"]] == "inactive" and self.occurs(n["trig"], m, I["b"], n["kind"] == "prohibition"): I["st"][n["id"]] = "active"
                for n in C["norms"]:
                    if n["until"] and I["st"][n["id"]] == "active" and self.occurs(n["until"], m, I["b"], n["kind"] == "permission"):
                        I["st"][n["id"]] = "discharged"
                        if n["id"] in C["deleg"]: I["held"][n["id"]].clear()
        approved = []
        for (creates, hid), keys in events.items():
            h = self.holds.get(hid)
            if h and h["state"] == "held" and h["approver"] == txt[0] and keys & h["insts"]:
                if creates == "approval": h["state"] = "approved"; approved.append(h)
                else: h["state"] = "rejected"; self.out.append((t, h["txt"], "deny", "rejected", h["id"], "rejected"))
        for h in sorted(approved, key=lambda h: h["n"]): self.release(h, t)
    def release(self, h, t):
        m, txt = h["m"], h["txt"]
        if self.kind(m) == "undeclared":
            self.out.append((t, txt, "deny", "undeclared_tool", h["id"], "approved")); return
        reasons = []
        for C in self.cs:
            mine = {k for k in h["insts"] if k[0] == C["id"]}
            if mine:
                running = {(C["id"], I["name"]) for I in self.run(C)}
                if mine - running: reasons.append("no_instance")
                elif any((C["id"], n["id"], I["name"]) not in h["grounds"] for n, I in self.grounds(C, m)): reasons.append("prohibition")
            elif self.governs(C, m):
                r = self.contract(C, m, txt)
                if r[0] != "allow": reasons.append(r[1])
        if reasons:
            self.out.append((t, txt, "deny", min(reasons, key=["no_instance", "prohibition", "default"].index), h["id"], "approved")); return
        self.out.append((t, txt, "allow", "approved", h["id"], "approved"))
        self.observe(m, txt, h["args"], t)
    # --- trace
    def tick(self, t):
        # times are exact (F.5): a time of the trace is the decimal number it is written as
        t = Fraction(repr(t))
        for h in sorted([h for h in self.holds.values() if h["state"] == "held" and h["deadline"] is not None and t > h["deadline"]], key=lambda h: (h["deadline"], h["n"])):
            h["state"] = "expired"; self.out.append((float(h["deadline"]), h["txt"], "deny", "expired", h["id"], "expired"))
    def start(self, cid, name, b):
        self.create(next(C for C in self.cs if C["id"] == cid), name, b)
    def end(self, cid, name, t):
        C = next(C for C in self.cs if C["id"] == cid); I = next(i for i in C["insts"] if i["name"] == name)
        I["run"] = False
        for h in sorted(self.holds.values(), key=lambda h: h["n"]):
            if h["state"] == "held" and (cid, name) in h["insts"]:
                h["state"] = "cancelled"; self.out.append((t, h["txt"], "deny", "cancelled", h["id"], "cancelled"))
    def message(self, frm, to, name, args, t):
        m = (pref(frm), pref(to), name); txt = (frm, to, name)
        dec, reason, appr, timeout, prov = self.decide(m, txt)
        hid = None
        if dec == "escalate":
            self.nh += 1; hid = f"h-{self.nh}"
            gr = [(C["id"], n["id"], I["name"]) for C in self.cs if self.governs(C, m) for n, I in self.grounds(C, m)]
            self.holds[hid] = dict(id=hid, n=self.nh, m=m, txt=txt, args=args, state="held", approver=appr,
                deadline=None if timeout is None else Fraction(repr(t)) + Fraction(timeout), grounds=set(gr), insts={(c, i) for c, _, i in gr})
        self.out.append((t, txt, dec, reason, hid, None) + ((tuple(map(tuple, prov)),) if prov else ()))
        if dec == "allow": self.observe(m, txt, args, t)
