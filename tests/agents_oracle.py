"""Independent model of G.4 to G.7, written from the text of Appendix G by
the reviewer of the runtime and adapted to the rules the review led to
(release decided again, governed observations). It shares no code with
`cadl`: it reads the YAML directly. It supports the default instance only,
no bindings, norms with a message content and message `trigger:` /
`until:`, delegations and holds, with the matching of G.4.

Not a test module: tests/test_agents_runtime.py compares the runtime with
it on generated traces."""

from __future__ import annotations

import re

import yaml
UNIT = {"ms": .001, "s": 1, "min": 60, "h": 3600}
def dur(x):
    m = re.fullmatch(r"(\d+)\s*(ms|s|min|h)", str(x)); return int(m[1]) * UNIT[m[2]]
def pref(s):
    s = s.replace(" ", ""); m = re.fullmatch(r"(\w+)(?:\[(.*)\])?", s)
    return (m[1], m[2])
def pev(s):
    a, rest = s.split("->"); b, n = rest.split(":"); n = n.strip().split("(")[0]
    return pref(a), pref(b), n
class Model:
    def __init__(self, text):
        d = yaml.safe_load(text)["sos"]
        self.actors = {}
        for a in d["actors"]:
            name, idx = pref(a["id"])
            rng = None
            if idx: lo, hi = idx.split(".."); rng = (int(lo), int(hi))
            ag = a.get("agent") or {}
            self.actors[name] = dict(rng=rng, kind=ag.get("kind"), tools=ag.get("tools", []))
        self.cs = []
        for c in d["contracts"]:
            if "enforcement" not in c: continue
            enf = c["enforcement"] or {}
            C = dict(id=c["id"], parties=[pref(p) for p in c["parties"]], default=enf.get("default"),
                     rules=[(pev(r["when"]), r["creates"]) for r in c.get("counts_as", [])], norms=[], deleg={})
            for n in c.get("norms", []):
                if n["kind"] not in ("permission", "prohibition"): continue
                od = n.get("on_denied") or {}
                C["norms"].append(dict(id=n["id"], kind=n["kind"], ev=pev(n["content"]),
                    trig=pev(n["trigger"]) if n.get("trigger") else None,
                    until=pev(n["until"]) if n.get("until") else None,
                    esc=od.get("escalate_to"), timeout=dur(od["timeout"]) if od.get("timeout") else None,
                    state="inactive" if n.get("trigger") else "active"))
            for g in c.get("delegations", []) or []:
                C["deleg"][g["norm"]] = dict(to=[pref(x) for x in g["to"]], grant=g["grant"], revoke=g.get("revoke"),
                    depth=g.get("max_depth", 1), until=pev(g["until"]) if g.get("until") else None, held={})  # holder -> chain
            self.cs.append(C)
        self.holds = {}; self.nh = 0; self.out = []
    # --- G.4
    def member(self, name, actor):
        an, ai = actor
        if an != name or name not in self.actors: return False
        rng = self.actors[name]["rng"]
        if rng is None: return ai is None
        return ai is not None and ai.isdigit() and rng[0] <= int(ai) <= rng[1]
    def match(self, ref, actor):
        name, idx = ref
        if not self.member(name, actor): return False
        if idx is None or idx == "*": return True
        if idx.isdigit(): return actor[1] == idx
        if ".." in idx:
            lo, hi = idx.split(".."); return int(lo) <= int(actor[1]) <= int(hi)
        raise NotImplementedError(idx)
    def mev(self, ev, m): return ev[2] == m[2] and self.match(ev[0], m[0]) and self.match(ev[1], m[1])
    def kind(self, m):
        r = self.actors.get(m[1][0])
        if not r or r["kind"] != "tool_server" or not self.member(m[1][0], m[1]): return "ordinary"
        return "tool" if m[2] in r["tools"] else "undeclared"
    def governs(self, C, m):
        party = lambda a: any(self.match(p, a) for p in C["parties"])
        return party(m[1]) and (self.kind(m) != "ordinary" or party(m[0]))
    def valid(self, C, nid, holder):
        g = C["deleg"].get(nid); n = next(x for x in C["norms"] if x["id"] == nid)
        ch = g["held"].get(holder) if g else None
        return bool(ch) and n["state"] == "active" and len(ch) - 1 <= g["depth"] and all(any(self.match(r, pref(a)) for r in g["to"]) for a in ch[1:])
    def grounds(self, m):
        return [(C["id"], n) for C in self.cs if self.governs(C, m) for n in C["norms"]
                if n["kind"] == "prohibition" and n["state"] == "active" and self.mev(n["ev"], m)]
    def decide_contract(self, C, m, txt):
        """G.5, step 3, for one governing contract."""
        k = self.kind(m)
        g = [n for n in C["norms"] if n["kind"] == "prohibition" and n["state"] == "active" and self.mev(n["ev"], m)]
        if g:
            appr = {n["esc"] for n in g}
            if None not in appr and len(appr) == 1 and pref(next(iter(appr))) != m[0]:
                return ("escalate", "prohibition", next(iter(appr)), min([n["timeout"] for n in g if n["timeout"]] or [None], key=lambda x: x if x is not None else 1e99), [])
            return ("deny", "prohibition", None, None, [])
        prov = []; ok = False
        for n in C["norms"]:
            if n["kind"] != "permission" or n["ev"][2] != m[2] or not self.match(n["ev"][1], m[1]): continue
            if n["state"] == "active" and self.match(n["ev"][0], m[0]): ok = True
            if n["id"] in C["deleg"] and self.valid(C, n["id"], txt[0]) and self.match(n["ev"][0], pref(C["deleg"][n["id"]]["held"][txt[0]][0])):
                ok = True; prov.append(C["deleg"][n["id"]]["held"][txt[0]])
        if ok: return ("allow", "permission", None, None, prov)
        dflt = C["default"] or ("deny" if k == "tool" else "allow")
        return (dflt, "default", None, None, [])
    def decide(self, m, txt):
        gov = [C for C in self.cs if self.governs(C, m)]
        if not gov: return "allow", "ungoverned", None, None, []
        k = self.kind(m)
        if k == "undeclared": return "deny", "undeclared_tool", None, None, []
        res = [self.decide_contract(C, m, txt) for C in gov]
        den = [r for r in res if r[0] == "deny"]
        esc = [r for r in res if r[0] == "escalate"]
        order = ["no_instance", "prohibition", "default"]
        if den: return "deny", min((r[1] for r in den), key=order.index), None, None, []
        if esc:
            if len({r[2] for r in esc}) > 1: return "deny", "prohibition", None, None, []
            return "escalate", "prohibition", esc[0][2], min((r[3] for r in esc if r[3] is not None), default=None), []
        prov = [p for r in res for p in r[4]]
        return "allow", ("permission" if any(r[1] == "permission" for r in res) else "default"), None, None, prov
    def recheck(self, h):
        """G.6, Release: the reason of the denial, or None."""
        hm = h["m"]
        if self.kind(hm) == "undeclared": return "undeclared_tool"
        with_ground = {c for c, _ in h["grounds"]}
        reasons = []
        for C in self.cs:
            if not self.governs(C, hm): continue
            if C["id"] in with_ground:
                if any((c, n["id"]) not in h["grounds"] for c, n in self.grounds(hm) if c == C["id"]):
                    reasons.append("prohibition")
            else:
                r = self.decide_contract(C, hm, h["txt"])
                if r[0] != "allow": reasons.append(r[1])
        return min(reasons, key=["no_instance", "prohibition", "default"].index) if reasons else None
    def observe(self, m, txt, args, t):
        approved = []
        seen = set()
        for C in self.cs:
            if not self.governs(C, m): continue      # G.5: not an observation for C
            for ev, creates in C["rules"]:
                if creates in ("approval", "rejection") and self.mev(ev, m):
                    hid = args[0] if args and isinstance(args[0], str) else None
                    h = self.holds.get(hid)
                    if (creates, hid) in seen: continue
                    if h and h["state"] == "held" and h["approver"] == txt[0] and C["id"] in {c for c, _ in h["grounds"]}:
                        seen.add((creates, hid))
                        if creates == "approval": h["state"] = "approved"; approved.append(h)
                        else:
                            h["state"] = "rejected"; h["close"] = ("deny", "rejected", t)
            if self.governs(C, m):
                for nid, g in C["deleg"].items():
                    n = next(x for x in C["norms"] if x["id"] == nid)
                    x, y = txt[0], txt[1]
                    if m[2] == g["grant"]:
                        ch = None
                        if self.match(n["ev"][0], m[0]):
                            if n["state"] == "active": ch = [x]
                        elif self.valid(C, nid, x): ch = g["held"][x]
                        if ch and any(self.match(r, m[1]) for r in g["to"]) and y not in ch and len(ch) - 1 < g["depth"] and y not in g["held"]:
                            g["held"][y] = ch + [y]
                    elif g["revoke"] and m[2] == g["revoke"]:
                        ch = g["held"].get(y)
                        if ch and x in ch[:-1]:
                            for h_ in [h_ for h_, c_ in g["held"].items() if y in c_]: del g["held"][h_]
            for nid, g in C["deleg"].items():
                if g["until"] and self.mev(g["until"], m): g["held"].clear()
            for n in C["norms"]:
                if n["trig"] and n["state"] == "inactive" and self.mev(n["trig"], m): n["state"] = "active"
            for n in C["norms"]:
                if n["until"] and n["state"] == "active" and self.mev(n["until"], m):
                    n["state"] = "discharged"
                    if n["id"] in C["deleg"]: C["deleg"][n["id"]]["held"].clear()
        # closing records of rejected, then releases
        for h in self.holds.values():
            if h.get("close") and not h.get("closed"):
                h["closed"] = True; self.out.append((t, h["txt"], "deny", "rejected", h["id"], "rejected"))
        for h in approved:
            hm = h["m"]
            denied = self.recheck(h)
            if denied: self.out.append((t, h["txt"], "deny", denied, h["id"], "approved")); continue
            self.out.append((t, h["txt"], "allow", "approved", h["id"], "approved"))
            self.observe(hm, h["txt"], h["args"], t)
    def tick(self, t):
        for h in sorted([h for h in self.holds.values() if h["state"] == "held" and h["deadline"] is not None and t > h["deadline"] + 1e-9], key=lambda h: (h["deadline"], h["n"])):
            h["state"] = "expired"; self.out.append((h["deadline"], h["txt"], "deny", "expired", h["id"], "expired"))
    def message(self, frm, to, name, args, t):
        self.tick(t)
        m = (pref(frm), pref(to), name); txt = (frm, to, name)
        dec, reason, appr, timeout, prov = self.decide(m, txt)
        hid = None
        if dec == "escalate":
            self.nh += 1; hid = f"h-{self.nh}"
            self.holds[hid] = dict(id=hid, n=self.nh, m=m, txt=txt, args=args, state="held", approver=appr,
                deadline=None if timeout is None else t + timeout, grounds={(c, n["id"]) for c, n in self.grounds(m)})
        self.out.append((t, txt, dec, reason, hid, None) + ((tuple(map(tuple, prov)),) if prov else ()))
        if dec == "allow": self.observe(m, txt, args, t)
