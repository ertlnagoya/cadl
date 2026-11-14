# >>> norms-monitor-library
# The step function of the norms monitor (Appendix F, F.5; Appendix D).
#
# This part is the same for every contract; `cadl codegen -t opa` copies it
# verbatim. The generated part above it defines `model` (the norms of the
# contract as data) and the predicates of the contract as the functions
# x0, x1, ... and the rules pred_now, pred_prev, msg_args, content_vals and
# item_val. No text of the .cadl file is part of this code.
#
# Query `result` with input {"state": S, "observation": O} (S is null or
# absent for the first observation), or {"state": S, "finish": true} to end
# the contract instance at its last observation. Optional: "bindings",
# "request" (for the first observation) and "quantities" (names of
# quantities, added to the known names). `result.state` is the next state;
# the host stores it and passes it with the next observation.

_U := {"$": "unknown"}

_D := {"$": "undecided"}

_MAX_T := {"$q": [9007199254740992, 0]}

# The range of the exact numbers, as in cadl norms-eval (F.5): at most 400
# significant digits, at most 400 decimals, an absolute value of at most
# 1.7976931348623157e308 (the largest finite double, _MAX_ABS).
# The scales of the model are below 4096 (cadl codegen refuses a product
# that could exceed it), so that pow10 and the operations below are
# defined for every number the monitor computes with.
_LIMIT := 400

_MAX_SCALE := 4095

_MAX_ABS := {"$q": [17976931348623157 * pow10(292), 0]}

_NUM_RE := `^(-?)([0-9]+)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?$`

_FORMAT := "cadl-norms-monitor-state/1"

_KINDS := ["activate", "suspend", "resume", "discharge"]

_FINAL := {"fulfilled", "violated", "discharged"}

# Unicode whitespace as Python's \s; a value key without it (F.5 trace format).
_WS := `[\t\n\x{0b}\f\r\x{1c}-\x{1f} \x{85}\x{a0}\x{1680}\x{2000}-\x{200a}\x{2028}\x{2029}\x{202f}\x{205f}\x{3000}]+`

_ACTOR_RE := `^[A-Za-z_][\p{L}\p{N}_]*(\[(0|[1-9][0-9]*)\])?$`

_NAME_RE := `^[A-Za-z_][\p{L}\p{N}_]*$`

_SPLIT_RE := `^([A-Za-z_][\p{L}\p{N}_]*)(?:\[(.*)\])?$`

_OBS_KEYS := {"t", "bindings", "request", "values", "messages", "state", "end", "amend"}

_MSG_KEYS := {"from", "to", "name", "args"}

# --- values: numbers, booleans, strings, and the markers below ------------------
# {"$": "unknown"}: no value (final); {"$": "undecided"}: later observations
# decide; {"$sym": text}: a name without value (an enum-like literal);
# {"$o": object}: a field of request that is an object; {"$q": [n, k]}: the
# exact decimal number n / 10^k (F.5: numbers and times are exact).

# --- exact decimal numbers ------------------------------------------------------

# 10^k for 0 <= k <= _MAX_SCALE, by repeated squaring (integers only)
_p0 := 10

_p1 := _p0 * _p0

_p2 := _p1 * _p1

_p3 := _p2 * _p2

_p4 := _p3 * _p3

_p5 := _p4 * _p4

_p6 := _p5 * _p5

_p7 := _p6 * _p6

_p8 := _p7 * _p7

_p9 := _p8 * _p8

_p10 := _p9 * _p9

_p11 := _p10 * _p10

_POWERS := [_p0, _p1, _p2, _p3, _p4, _p5, _p6, _p7, _p8, _p9, _p10, _p11]

pow10(k) := product([_POWERS[i] | some i in numbers.range(0, 11); bits.and(k, bits.lsh(1, i)) != 0]) if {
	k >= 0
	k <= _MAX_SCALE
}

# a string of decimal digits as an integer, in chunks that to_number reads exactly
digits_int(s) := sum([digit_chunk(s, i) | some i in numbers.range(0, floor((count(s) - 1) / 18))])

digit_chunk(s, i) := to_number(substring(s, i * 18, 18)) * pow10(rest) if {
	rest := count(s) - (i * 18 + count(substring(s, i * 18, 18)))
}

# A number of the input as the decimal it is written as (its JSON text):
# n / 10^k with mantissa digits m and exponent e of its last digit.
# Undefined outside the range of the exact numbers.
dec(v) := q if {
	q := dec_digits(v)
	qle(q, _MAX_ABS)
	qle({"$q": [0 - qn(_MAX_ABS), 0]}, q)
}

dec_digits(v) := {"$q": [0, 0]} if {
	m := regex.find_all_string_submatch_n(_NUM_RE, json.marshal(v), 1)[0]
	trim_left(concat("", [m[2], m[3]]), "0") == ""
	exp_of(m[4]) - count(m[3]) >= 0 - _LIMIT
} else := {"$q": [sgn(m[1]) * digits_int(mant) * pow10(e), 0]} if {
	m := regex.find_all_string_submatch_n(_NUM_RE, json.marshal(v), 1)[0]
	mant := concat("", [m[2], m[3]])
	e := exp_of(m[4]) - count(m[3])
	in_range(mant, e)
	e >= 0
} else := {"$q": [sgn(m[1]) * digits_int(mant), 0 - e]} if {
	m := regex.find_all_string_submatch_n(_NUM_RE, json.marshal(v), 1)[0]
	mant := concat("", [m[2], m[3]])
	e := exp_of(m[4]) - count(m[3])
	in_range(mant, e)
	e < 0
}

sgn(s) := -1 if {
	s == "-"
} else := 1

in_range(mant, e) if {
	nd := count(trim_left(mant, "0"))
	nd <= _LIMIT
	e >= 0 - _LIMIT
	nd + e <= 309
}


exp_of(e) := 0 if {
	e == ""
} else := to_number(trim_left(e, "+"))

qn(x) := x["$q"][0]

qk(x) := x["$q"][1]

# the numerators of a and b at the larger of their scales
qal(a, b) := [qn(a) * pow10(k - qk(a)), qn(b) * pow10(k - qk(b)), k] if {
	k := max([qk(a), qk(b)])
}

qadd(a, b) := {"$q": [x[0] + x[1], x[2]]} if x := qal(a, b)

qsub(a, b) := {"$q": [x[0] - x[1], x[2]]} if x := qal(a, b)

qmul(a, b) := {"$q": [qn(a) * qn(b), qk(a) + qk(b)]}

# -1, 0 or 1
qcmp(a, b) := -1 if {
	x := qal(a, b)
	x[0] < x[1]
} else := 1 if {
	x := qal(a, b)
	x[0] > x[1]
} else := 0 if {
	x := qal(a, b)
	x[0] == x[1]
}

qle(a, b) if qcmp(a, b) <= 0

qmax0(a) := a if {
	qn(a) > 0
} else := {"$q": [0, 0]}

# for the reader of a result (the state keeps the exact value)
qf(x) := qn(x) if {
	qk(x) == 0
} else := qn(x) / pow10(qk(x))

qz := {"$q": [0, 0]}

vk(v) := "U" if {
	is_object(v)
	v["$"] == "unknown"
} else := "D" if {
	is_object(v)
	v["$"] == "undecided"
} else := "S" if {
	is_object(v)
	is_string(v["$sym"])
} else := "N" if {
	is_object(v)
	is_array(v["$q"])
} else := "B" if {
	is_boolean(v)
} else := "T" if {
	is_string(v)
} else := "X"

is_mark(v) if vk(v) in {"U", "D"}

truth(v) := v if {
	vk(v) in {"U", "D", "B"}
} else := false if {
	vk(v) == "N"
	qn(v) == 0
} else := true if {
	vk(v) == "N"
} else := _U

and3(l, r) := false if {
	l == false
} else := false if {
	r == false
} else := _D if {
	vk(l) == "D"
} else := _D if {
	vk(r) == "D"
} else := _U if {
	vk(l) == "U"
} else := _U if {
	vk(r) == "U"
} else := true

or3(l, r) := true if {
	l == true
} else := true if {
	r == true
} else := _D if {
	vk(l) == "D"
} else := _D if {
	vk(r) == "D"
} else := _U if {
	vk(l) == "U"
} else := _U if {
	vk(r) == "U"
} else := false

not3(v) := v if {
	is_mark(v)
} else := false if {
	v == true
} else := true

plain(v) := v["$sym"] if {
	vk(v) == "S"
} else := v

eq(a, b) := false if {
	is_boolean(a) != is_boolean(b)
} else := r if {
	vk(a) == "N"
	vk(b) == "N"
	r := qcmp(a, b) == 0
} else := false if {
	vk(a) == "N"
} else := false if {
	vk(b) == "N"
} else := r if {
	r := plain(a) == plain(b)
}

sym_ok(o, x) if {
	o in {"==", "!="}
	vk(x) == "T"
}

sym_bad(o, s, x) if {
	vk(s) == "S"
	not sym_ok(o, x)
}

numeric(v) if vk(v) in {"N", "B"}

num(v) := {"$q": [1, 0]} if {
	v == true
} else := qz if {
	v == false
} else := v

# a comparison of two numbers
ordq(o, a, b) := ordc(o, qcmp(a, b), 0)

ordc(o, a, b) := r if {
	o == "<"
	r := a < b
} else := r if {
	o == "<="
	r := a <= b
} else := r if {
	o == ">"
	r := a > b
} else := r if {
	o == ">="
	r := a >= b
}

binop(o, l, r) := _U if {
	vk(l) == "U"
} else := _U if {
	vk(r) == "U"
} else := _D if {
	vk(l) == "D"
} else := _D if {
	vk(r) == "D"
} else := _U if {
	sym_bad(o, l, r)
} else := _U if {
	sym_bad(o, r, l)
} else := eq(l, r) if {
	o == "=="
} else := not3(eq(l, r)) if {
	o == "!="
} else := ordq(o, num(l), num(r)) if {
	o in {"<", "<=", ">", ">="}
	numeric(l)
	numeric(r)
} else := ordc(o, l, r) if {
	o in {"<", "<=", ">", ">="}
	vk(l) == "T"
	vk(r) == "T"
} else := _U if {
	o in {"<", "<=", ">", ">="}
} else := _U if {
	not vk(l) == "N"
} else := _U if {
	not vk(r) == "N"
} else := x if {
	o == "+"
	x := qadd(l, r)
} else := x if {
	o == "-"
	x := qsub(l, r)
} else := x if {
	o == "*"
	x := qmul(l, r)
} else := _U

nosym(i) := _U if {
	vk(i) == "S"
} else := i

in_items(l, items) := items if {
	vk(l) == "T"
} else := [nosym(i) | some i in items]

in3(l, items) := l if {
	is_mark(l)
} else := _U if {
	vk(l) == "S"
} else := true if {
	some i in in_items(l, items)
	not is_mark(i)
	eq(l, i) == true
} else := _D if {
	some i in in_items(l, items)
	vk(i) == "D"
} else := _U if {
	some i in in_items(l, items)
	vk(i) == "U"
} else := false

# --- the temporal operators (F.4) ---------------------------------------------

# within (dec true) and throughout (dec false) from the values of the window
temporal(vals, over, dec) := dec if {
	some v in vals
	v == dec
} else := _D if {
	not over
} else := _D if {
	some v in vals
	vk(v) == "D"
} else := _U if {
	some v in vals
	vk(v) == "U"
} else := false if {
	dec == true
} else := true

# positions of the window [t_p, t_p + d] and whether it is over
window(p, d) := [j | some j in numbers.range(p, last); qle(buf[j].t, qadd(buf[p].t, d))]

over(p, d) if not qle(buf[last].t, qadd(buf[p].t, d))

u_stop(ps) := min([i | some i, v in ps; v == false]) if {
	some v in ps
	v == false
} else := count(ps) - 1

u_prefix(ps, i) := _D if {
	some j, v in ps
	j < i
	vk(v) == "D"
} else := _U if {
	some j, v in ps
	j < i
	vk(v) == "U"
} else := true

u_worst(q, pre) := _D if {
	vk(q) == "D"
} else := _D if {
	vk(pre) == "D"
} else := _U if {
	vk(q) == "U"
} else := _U if {
	vk(pre) == "U"
}

u_contrib(qs, ps, i) := u_worst(_U, u_prefix(ps, i)) if {
	qs[i] == true
	u_prefix(ps, i) != true
} else := u_worst(qs[i], u_prefix(ps, i)) if {
	is_mark(qs[i])
}

until3(qs, ps, ov) := true if {
	some i, q in qs
	i <= u_stop(ps)
	q == true
	u_prefix(ps, i) == true
} else := _D if {
	not false in ps
	not ov
} else := _D if {
	some i in numbers.range(0, u_stop(ps))
	vk(u_contrib(qs, ps, i)) == "D"
} else := _U if {
	some i in numbers.range(0, u_stop(ps))
	vk(u_contrib(qs, ps, i)) == "U"
} else := false

# u_worst of a marker and true is the marker; of two markers the worse:
# called above only where at least one is a marker or the prefix is.

# --- the observation -----------------------------------------------------------

default _st := null

_st := input.state if input.state != null

finishing if input.finish == true

obs := object.get(input, "observation", null)

lc := model.lifecycle

init_norm(i, nd) := {
	"id": nd.id, "state": "inactive", "verdict": "pending", "at": null, "act": null,
	"since": null, "before": qz, "from": 0, "cands": [],
}

init_item := {"value": null, "seen": false, "verdict": "pending", "time": null}

init_state := {
	"format": _FORMAT, "contract": model.contract, "n": 0, "t": null, "ended": false,
	"end_index": null, "ignored": 0, "bindings": {}, "request": {}, "known": [],
	"values": {}, "lc": null, "buffer": [],
	"norms": [init_norm(i, nd) | some i, nd in model.norms],
	"items": {
		"assume": [init_item | some _ in model.items.assume],
		"guarantee": [init_item | some _ in model.items.guarantee],
	},
	"failed_at": null,
}

S := _st if {
	_st != null
} else := init_state

n := S.n

first if n == 0

t := obs.t

# the time of the observation, exact
tq := dec(obs.t)

has(o, k) if k in object.keys(o)

canon(k) := regex.replace(k, _WS, "")

B := obs.bindings if {
	first
	has(obs, "bindings")
} else := input.bindings if {
	first
	is_object(input.bindings)
} else := {} if {
	first
} else := S.bindings

R := obs.request if {
	first
	has(obs, "request")
} else := input.request if {
	first
	is_object(input.request)
} else := {} if {
	first
} else := S.request

obs_values := object.get(obs, "values", {})

vals_in := {canon(k): v | some k, v in obs_values}

V := object.union(S.values, vals_in)

quantities := [canon(q) | some q in input.quantities; is_string(q)] if {
	is_array(input.quantities)
} else := []

known := ({k | some k in S.known} | {k | some k, _ in vals_in}) | {q | some q in quantities}

lc_before := lc.initial if {
	lc != null
	first
} else := S.lc if {
	lc != null
} else := null

lc_after := obs.state if {
	lc != null
	has(obs, "state")
} else := lc_before

msgs := [{"from": m.from, "to": m.to, "name": m.name, "args": object.get(m, "args", [])} |
	some m in object.get(obs, "messages", [])
]

entry := {"i": n, "t": tq, "values": V, "st": lc_before}

buf := array.concat(S.buffer, [entry])

last := count(buf) - 1

base := buf[0].i

pos(k) := k - base

# --- names ---------------------------------------------------------------------

# a value of the input with its number exact
val(v) := dec(v) if is_number(v)

else := v

lookup(vals, key) := _U if {
	not has(vals, key)
} else := _U if {
	vals[key] == null
} else := val(vals[key])

lookup2(vals, key, stripped) := lookup(vals, key) if {
	has(vals, key)
} else := lookup(vals, stripped)

fmt_binding(v) := v if {
	is_string(v)
} else := format_int(v, 10)

actor_text(a, idx) := a if {
	idx == null
} else := sprintf("%s[%s]", [a, idx.lit]) if {
	is_string(idx.lit)
} else := sprintf("%s[%s]", [a, fmt_binding(B[idx.var])]) if {
	has(B, idx.var)
} else := sprintf("%s[%s]", [a, idx.var])

bound_index(idx) if idx == null

bound_index(idx) if is_string(idx.lit)

bound_index(idx) if has(B, idx.var)

name_val(nm, p) := val(B[nm]) if {
	has(B, nm)
} else := buf[p].t if {
	nm == "now"
} else := _U if {
	nm == "state"
	lc != null
	buf[p].st == null
} else := buf[p].st if {
	nm == "state"
	lc != null
} else := lookup(buf[p].values, nm) if {
	nm in known
} else := {"$sym": nm}

mem_val(a, idx, m, p) := lookup(buf[p].values, concat(".", [a, m])) if {
	idx == null
} else := lookup2(buf[p].values, concat(".", [actor_text(a, idx), m]), concat(".", [a, m]))

req_val(path) := _U if {
	v := object.get(R, path, null)
	v == null
} else := {"$o": v} if {
	v := object.get(R, path, null)
	is_object(v)
} else := val(object.get(R, path, null))

# --- messages and actors -------------------------------------------------------

split_actor(actual) := regex.find_all_string_submatch_n(_SPLIT_RE, actual, 1)[0]

is_member(name, idx) if {
	decl := model.actors[name]
	idx == ""
	decl[0] == false
}

is_member(name, idx) if {
	decl := model.actors[name]
	idx != ""
	decl[0] == true
	regex.match(`^[0-9]+$`, idx)
	in_bounds(to_number(idx), decl[1], decl[2])
}

in_bounds(i, lo, hi) if {
	lo_ok(i, lo)
	hi_ok(i, hi)
}

lo_ok(_, lo) if lo == null

lo_ok(i, lo) if i >= lo

hi_ok(_, hi) if hi == null

hi_ok(i, hi) if i <= hi

actor_match(pat, actual) if {
	m := split_actor(actual)
	m[1] == pat.name
	is_member(m[1], m[2])
	pat_ok(pat, m[2], actual)
}

pat_ok(pat, idx, _) if {
	pat.index.any == true
	idx != ""
	pat.index.range == null
}

pat_ok(pat, idx, _) if {
	pat.index.any == true
	idx != ""
	pat.index.range != null
	in_bounds(to_number(idx), pat.index.range[0], pat.index.range[1])
}

pat_ok(pat, _, actual) if {
	not pat.index.any
	bound_index(pat.index)
	actual == actor_text(pat.name, pat.index)
}

pat_ok(pat, _, _) if {
	not pat.index.any
	not bound_index(pat.index)
}

eq_raw(w, a) := false if {
	is_mark(w)
} else := r if {
	vk(w) == "X"
	r := w["$o"] == a
} else := false if {
	is_object(a)
} else := eq(w, val(a))

args_ok(_, term, _) if term.args == null

args_ok(j, term, m) if {
	term.args != null
	want := msg_args[j]
	count(want) == count(m.args)
	every i, w in want {
		eq_raw(w, m.args[i]) == true
	}
}

msg_matches[j] := [m |
	some m in msgs
	m.name == term.name
	actor_match(term.sender, m.from)
	actor_match(term.receiver, m.to)
	args_ok(j, term, m)
] if {
	some j, term in model.terms
	term.kind == "message"
	not term.error
}

desc(m) := sprintf("%s -> %s : %s", [m.from, m.to, m.name])

edge(j) if {
	pred_now[j] == true
	first
}

edge(j) if {
	pred_now[j] == true
	not first
	pred_prev[j] != true
}

# --- counts-as (step 1) -------------------------------------------------------

cond_ok(r) if r["if"] == null

cond_ok(r) if pred_now[r["if"]] == true

rule_events(r) := [{"t": t, "name": r.creates, "performer": m.from, "source": desc(m)} |
	some m in msg_matches[r.when]
] if {
	model.terms[r.when].kind == "message"
} else := [{"t": t, "name": r.creates, "performer": null, "source": model.terms[r.when].text}] if {
	edge(r.when)
} else := []

created := [e |
	some r in model.rules
	not r.error
	cond_ok(r)
	some e in rule_events(r)
]

occurs(j) if {
	term := model.terms[j]
	term.kind == "event"
	not term.error
	some e in created
	e.name == term.text
}

occurs(j) if {
	model.terms[j].kind == "message"
	count(msg_matches[j]) > 0
}

occurs(j) if {
	model.terms[j].kind == "predicate"
	not model.terms[j].error
	edge(j)
}

# --- norm instances ------------------------------------------------------------

elapsed(rec, tt) := qadd(rec.before, qsub(tt, rec.since)) if {
	rec.state == "active"
	rec.since != null
} else := rec.before

verdict_of(old, new, fa) := "excused" if {
	new == "violated"
	fa != null
} else := "violated" if {
	new == "violated"
} else := "fulfilled" if {
	new in {"fulfilled", "discharged"}
} else := old

# the state of a norm instance changes at time tt, observation k (_set of the engine)
setst(rec, new, tt, k, fa) := object.union(rec, {
	"state": new,
	"verdict": verdict_of(rec.verdict, new, fa),
	"before": elapsed(rec, tt),
	"since": since_of(rec, new, tt),
	"at": at_of(rec, new, tt),
	"act": at_of({"at": rec.act}, new, k),
	"cands": cands_of(rec, new),
})

since_of(rec, new, tt) := tt if {
	new == "active"
} else := rec.since

at_of(rec, new, x) := x if {
	new == "active"
	rec.at == null
} else := rec.at

cands_of(rec, new) := [] if {
	new in _FINAL
} else := rec.cands

set_now(rec, new) := setst(rec, new, tq, n, FA)

# step 0: the norms without trigger that are not activated by a violation or power
N0 := [start_norm(S.norms[i], nd) | some i, nd in model.norms] if {
	first
} else := S.norms

start_norm(rec, nd) := set_now(rec, "active") if {
	nd.trigger == null
	not nd.target
} else := rec

# --- powers (step 2) -----------------------------------------------------------

powers := [i | some i, nd in model.norms; nd.kind == "power"]

event_power_texts := {model.terms[model.norms[i].content].text |
	some i in powers
	model.terms[model.norms[i].content].kind == "event"
}

event_acts := [{"c": e.name, "perf": e.performer, "desc": concat("", ["institutional event ", e.name])} |
	some e in created
	e.performer != null
	e.name in event_power_texts
]

msg_act_cands := [{"c": model.terms[model.norms[p].content].text, "perf": m.from, "d": desc(m)} |
	some p in powers
	model.terms[model.norms[p].content].kind == "message"
	some m in msg_matches[model.norms[p].content]
]

dup_before(ci, a) if {
	some cj, b in msg_act_cands
	cj < ci
	b.c == a.c
	b.d == a.d
}

msg_acts := [{"c": a.c, "perf": a.perf, "desc": concat("", ["message ", a.d])} |
	some ci, a in msg_act_cands
	not dup_before(ci, a)
]

acts := array.concat(event_acts, msg_acts)

is_bearer(h, perf) if {
	perf != null
	b := model.norms[h].bearer
	b != null
	bound_index(b.index)
	perf == actor_text(b.name, b.index)
}

is_bearer(h, perf) if {
	perf != null
	b := model.norms[h].bearer
	b != null
	not bound_index(b.index)
	m := split_actor(perf)
	m[1] == b.name
	is_member(m[1], m[2])
}

holders(a) := [h |
	some h in powers
	model.terms[model.norms[h].content].text == a.c
	N0[h].state == "active"
	is_bearer(h, a.perf)
]

unauthorised := [{"t": t, "act": a.desc, "performer": a.perf,
	"reason": "no matching power of the performer is active"} |
	some a in acts
	count(holders(a)) == 0
]

exercised := [{"t": t, "power": model.norms[h].id, "performer": a.perf, "act": a.desc} |
	some a in acts
	some h in holders(a)
]

ops := [[target, kname] |
	some a in acts
	some h in holders(a)
	some kname in _KINDS
	some target in model.norms[h].effects[kname]
]

power_seq(i) := [o[1] | some o in ops; o[0] == i]

first_index(seq, x) := min([i | some i, v in seq; v == x])

last_toggle(seq) := "suspended" if {
	toggles := [v | some v in seq; v in {"suspend", "resume"}]
	toggles[count(toggles) - 1] == "suspend"
} else := "active" if {
	toggles := [v | some v in seq; v in {"suspend", "resume"}]
	toggles[count(toggles) - 1] == "resume"
}

bc_apply(rec, seq) := set_now(rec, "discharged") if {
	"discharge" in seq
} else := set_now(rec, f) if {
	f := last_toggle(seq)
	f != rec.state
} else := rec

power_apply(rec, seq) := rec if {
	count(seq) == 0
} else := bc_apply(set_now(rec, "active"), array.slice(seq, q + 1, count(seq))) if {
	rec.state == "inactive"
	q := first_index(seq, "activate")
} else := bc_apply(rec, seq) if {
	rec.state in {"active", "suspended"}
} else := rec

N1 := [power_apply(N0[i], power_seq(i)) | some i, _ in model.norms]

# --- triggers (step 3) ---------------------------------------------------------

N2 := [trigger_apply(N1[i], i, nd) | some i, nd in model.norms]

trigger_apply(rec, i, nd) := set_now(rec, "active") if {
	nd.trigger != null
	occurs(nd.trigger)
	rec.state == "inactive"
} else := rec

# --- contents, deadlines and until (step 4) -------------------------------------

cval(i, k) := content_vals[i][k] if {
	model.terms[model.norms[i].content].kind == "predicate"
} else := false if {
	k != n
} else := true if {
	occurs(model.norms[i].content)
} else := false

is_open(rec, nd) if nd.deadline == null

is_open(rec, nd) if qle(elapsed(rec, tq), nd.deadline)

judge_cands(rec, nd) := array.concat(rec.cands, [n]) if {
	is_open(rec, nd)
} else := rec.cands

hit(i, cands) if {
	some k in cands
	cval(i, k) == true
}

content_judge(rec, i, nd) := set_now(rec, "fulfilled") if {
	hit(i, judge_cands(rec, nd))
	nd.kind == "obligation"
} else := set_now(rec, "violated") if {
	hit(i, judge_cands(rec, nd))
} else := set_now(rec, "violated") if {
	not is_open(rec, nd)
	count(undecided(i, judge_cands(rec, nd))) == 0
	nd.kind == "obligation"
} else := set_now(rec, "fulfilled") if {
	not is_open(rec, nd)
	count(undecided(i, judge_cands(rec, nd))) == 0
} else := object.union(rec, {"cands": undecided(i, judge_cands(rec, nd))})

undecided(i, cands) := [k | some k in cands; vk(cval(i, k)) == "D"]

judge_content(rec, i, nd) := content_judge(rec, i, nd) if {
	rec.state == "active"
	nd.kind in {"obligation", "prohibition"}
} else := rec

judge_until(rec, nd) := set_now(rec, "discharged") if {
	rec.state in {"active", "suspended"}
	nd.until != null
	occurs(nd.until)
} else := rec

judge(rec, i, nd) := judge_until(judge_content(rec, i, nd), nd) if {
	n >= rec.from
} else := rec

N3 := [judge(N2[i], i, nd) | some i, nd in model.norms]

viol := [i | some i, _ in model.norms; N3[i].state == "violated"; N2[i].state != "violated"]

# --- violations and reparations (step 5) ----------------------------------------

terminal_before if {
	lc != null
	lc_before in {s | some s in lc.terminal}
}

rep_act_true(vp) if {
	v := viol[vp]
	r := model.norms[v].reparation
	r != null
	N3[r].state == "inactive"
	not rep_earlier(vp, r)
}

rep_earlier(vp, r) if {
	some vq, v in viol
	vq < vp
	model.norms[v].reparation == r
}

violation_rec(v, activated, transition) := {
	"t": t, "norm": model.norms[v].id, "severity": model.norms[v].severity,
	"verdict": N3[v].verdict, "sanction": model.norms[v].sanction,
	"reparation": model.norms[v].reparation_id, "reparation_activated": activated,
	"transition": transition,
}

transition_of(v) := model.norms[v].transition if {
	model.norms[v].transition != null
	not terminal_before
} else := null

step_violations := [violation_rec(v, rep_flag(vp), transition_of(v)) | some vp, v in viol]

N4 := [rep_apply(N3[i], i) | some i, _ in model.norms]

rep_apply(rec, i) := object.union(set_now(rec, "active"), {"from": n + 1}) if {
	some vp, v in viol
	rep_act_true(vp)
	model.norms[v].reparation == i
} else := rec

rep_flag(vp) := true if {
	rep_act_true(vp)
} else := false

content_set(i) := {k | some k in S.norms[i].cands} | {n}

content_of(v) := false if {
	vk(v) == "U"
} else := v

over_v(p, d) := true if {
	over(p, d)
} else := false

# --- the end of the contract instance (step 6) -----------------------------------

end_reason := "terminal" if {
	lc != null
	lc_after in {s | some s in lc.terminal}
} else := "end" if {
	obs.end == true
}

settle(rec, nd, tt, k, fa) := rec if {
	rec.state in _FINAL
} else := setst(rec, "discharged", tt, k, fa) if {
	rec.state in {"active", "suspended"}
	rec.act == k
} else := rec if {
	rec.state in {"active", "suspended"}
	nd.kind in {"obligation", "prohibition"}
	count(rec.cands) > 0
} else := setst(rec, "violated", tt, k, fa) if {
	rec.state in {"active", "suspended"}
	nd.kind == "obligation"
	nd.deadline == null
} else := setst(rec, "discharged", tt, k, fa) if {
	rec.state in {"active", "suspended"}
	nd.kind == "obligation"
	qle(elapsed(rec, tt), nd.deadline)
} else := setst(rec, "violated", tt, k, fa) if {
	rec.state in {"active", "suspended"}
	nd.kind == "obligation"
} else := setst(rec, "fulfilled", tt, k, fa) if {
	rec.state in {"active", "suspended"}
	nd.kind == "prohibition"
} else := setst(rec, "discharged", tt, k, fa)

settled(norms, tt, k, fa) := [settle(norms[i], nd, tt, k, fa) | some i, nd in model.norms]

late_violations(before, after, tt) := [{
	"t": qf(tt), "norm": nd.id, "severity": nd.severity, "verdict": after[i].verdict,
	"sanction": nd.sanction, "reparation": nd.reparation_id,
	"reparation_activated": false, "transition": null,
} |
	some i, nd in model.norms
	after[i].state == "violated"
	before[i].state != "violated"
]

N5 := settled(N4, tq, n, FA) if {
	ended_now
} else := N4

# --- assume: and guarantee: items -------------------------------------------------

item_step(it, md, id) := it if {
	md.error != null
} else := it if {
	md.unsupported
} else := it if {
	it.value != null
} else := item_temporal(it, v) if {
	md.temporal
	v := item_val[id]
} else := item_state(it, v) if {
	not md.temporal
	v := item_val[id]
} else := it

item_temporal(it, v) := it if {
	vk(v) == "D"
} else := object.union(it, {"value": "undefined"}) if {
	vk(v) == "U"
} else := object.union(it, {"value": v, "time": t})

item_state(it, v) := object.union(it, {"seen": true}) if {
	v == true
} else := object.union(it, {"value": false, "time": t}) if {
	v == false
} else := it

I_assume := [item_step(S.items.assume[i], md, sprintf("a%d", [i])) | some i, md in model.items.assume]

FA := S.failed_at if {
	S.failed_at != null
} else := t if {
	some i, it in I_assume
	it.value == false
	S.items.assume[i].value != false
} else := null

I_guarantee := [guarantee_verdict(item_step(S.items.guarantee[i], md, sprintf("g%d", [i])), S.items.guarantee[i]) |
	some i, md in model.items.guarantee
]

guarantee_verdict(it, old) := object.union(it, {"verdict": "excused"}) if {
	it.value == false
	old.value != false
	FA != null
} else := object.union(it, {"verdict": "violated"}) if {
	it.value == false
	old.value != false
} else := it

# _finish_items of the engine: a decided guarantee gets its verdict, and at the
# end a state predicate true at every observation with a value holds
finish_item(it, md, ended) := object.union(it, {"value": true, "verdict": "fulfilled", "time": end_t}) if {
	ended
	it.value == null
	not md.temporal
	md.error == null
	not md.unsupported
	it.seen
} else := object.union(it, {"verdict": "fulfilled"}) if {
	it.value == true
} else := it

finish_items(items, ended) := {
	"assume": [finish_item(items.assume[i], md, ended) | some i, md in model.items.assume],
	"guarantee": [finish_item(items.guarantee[i], md, ended) | some i, md in model.items.guarantee],
}

# --- marks, changes and verdicts -------------------------------------------------

assume_status(it) := "holds" if {
	it.value == true
} else := "failed" if {
	it.value == false
} else := "pending"

changes(old, new, tq_) := array.concat(array.concat(
	[{"t": qf(tq_), "kind": "norm", "id": nd.id, "state": new.norms[i].state,
		"verdict": new.norms[i].verdict, "previous_state": old.norms[i].state,
		"previous_verdict": old.norms[i].verdict} |
		some i, nd in model.norms
		[old.norms[i].state, old.norms[i].verdict] != [new.norms[i].state, new.norms[i].verdict]
	],
	[{"t": qf(tq_), "kind": "assumption", "index": i + 1, "text": md.text,
		"status": assume_status(new.items.assume[i]), "previous": assume_status(old.items.assume[i])} |
		some i, md in model.items.assume
		assume_status(new.items.assume[i]) != assume_status(old.items.assume[i])
	],
),
	[{"t": qf(tq_), "kind": "guarantee", "index": i + 1, "text": md.text,
		"verdict": new.items.guarantee[i].verdict, "previous": old.items.guarantee[i].verdict} |
		some i, md in model.items.guarantee
		new.items.guarantee[i].verdict != old.items.guarantee[i].verdict
	])

verdicts(st) := {
	"norms": {nd.id: {"state": st.norms[i].state, "verdict": st.norms[i].verdict} | some i, nd in model.norms},
	"assumptions": [{"index": i + 1, "text": md.text, "status": assume_status(st.items.assume[i])} |
		some i, md in model.items.assume
	],
	"guarantees": [{"index": i + 1, "text": md.text, "verdict": st.items.guarantee[i].verdict,
		"monitored": md.unsupported == false} |
		some i, md in model.items.guarantee
	],
	"ok": ok_of(st),
}

ok_of(st) := false if {
	some rec in st.norms
	rec.verdict == "violated"
} else := false if {
	some it in st.items.guarantee
	it.verdict == "violated"
} else := true

due(rec, nd, tt) := qadd(tt, qmax0(qsub(nd.deadline, elapsed(rec, tt))))

next_deadline(st) := qf(first) if {
	not st.ended
	dues := [due(st.norms[i], nd, st.t) |
		some i, nd in model.norms
		nd.deadline != null
		nd.kind in {"obligation", "prohibition"}
		st.norms[i].state == "active"
		qle(elapsed(st.norms[i], st.t), nd.deadline)
	]
	some first in dues
	every d in dues {
		qle(first, d)
	}
} else := null

# --- the new state -------------------------------------------------------------------

ended_now := true if {
	end_reason
} else := false

end_t := t if not finishing

end_t := qf(S.t) if finishing

items_now := finish_items({"assume": I_assume, "guarantee": I_guarantee}, ended_now)

undecided_temporal_item if {
	some i, md in model.items.assume
	md.temporal
	md.error == null
	items_now.assume[i].value == null
}

undecided_temporal_item if {
	some i, md in model.items.guarantee
	md.temporal
	md.error == null
	not md.unsupported
	items_now.guarantee[i].value == null
}

keep_from := min(({n} | {k | some rec in N5; some k in rec.cands}) | {0 | undecided_temporal_item})

new_buffer := [] if {
	ended_now
} else := [e | some e in buf; e.i >= keep_from]

step_state := {
	"format": _FORMAT, "contract": model.contract, "n": n + 1, "t": tq,
	"ended": ended_now, "end_index": end_index, "ignored": S.ignored,
	"bindings": B, "request": R, "known": sort([k | some k in known]), "values": V,
	"lc": lc_after, "buffer": new_buffer, "norms": N5, "items": items_now, "failed_at": FA,
}

end_index := n if {
	ended_now
} else := null

all_violations := array.concat(step_violations, late_violations(N4, N5, tq))

step_result := {
	"state": step_state,
	"changes": changes(S, step_state, tq),
	"violations": all_violations,
	"institutional_events": created,
	"powers_exercised": exercised,
	"unauthorised": unauthorised,
	"verdicts": verdicts(step_state),
	"ended": ended_now,
	"next_deadline": next_deadline(step_state),
}

# end() of the Python monitor: settle at the last observation received
finish_norms := settled(S.norms, S.t, S.n - 1, S.failed_at)

finish_state := object.union(S, {
	"ended": true, "end_index": S.n - 1, "buffer": [],
	"norms": finish_norms,
	"items": finish_items(S.items, true),
})

finish_result := {
	"state": finish_state,
	"changes": changes(S, finish_state, S.t),
	"violations": late_violations(S.norms, finish_norms, S.t),
	"institutional_events": [],
	"powers_exercised": [],
	"unauthorised": [],
	"verdicts": verdicts(finish_state),
	"ended": true,
	"next_deadline": null,
}

ended_result := {
	"state": S,
	"changes": [],
	"violations": [],
	"institutional_events": [],
	"powers_exercised": [],
	"unauthorised": [],
	"verdicts": verdicts(S),
	"ended": true,
	"next_deadline": null,
}

ignored_result := {
	"state": object.union(S, {"ignored": S.ignored + 1, "t": tq}),
	"changes": [],
	"violations": [],
	"institutional_events": [],
	"powers_exercised": [],
	"unauthorised": [],
	"verdicts": verdicts(S),
	"ended": true,
	"next_deadline": null,
}

# --- checks of the input (the errors of `cadl norms-eval`, and what this monitor
# does not accept) ---------------------------------------------------------------

errs[0] := "the state was not made by the monitor of this contract" if {
	_st != null
	not _st.format == _FORMAT
}

errs[0] := "the state was not made by the monitor of this contract" if {
	_st != null
	_st.contract != model.contract
}

errs[1] := "the contract instance has no observation" if {
	finishing
	n == 0
}

errs[2] := "an observation must be a JSON object" if {
	not finishing
	not is_object(obs)
}

errs[3] := sprintf("unknown key(s) %s", [concat(", ", sort([k | some k, _ in obs; not k in _OBS_KEYS]))]) if {
	not finishing
	is_object(obs)
	some k, _ in obs
	not k in _OBS_KEYS
}

errs[4] := "'t' must be a number (seconds)" if {
	not finishing
	is_object(obs)
	not is_number(obs.t)
}

errs[5] := "'t' must be a finite number of at most 2**53 in absolute value" if {
	not finishing
	is_number(obs.t)
	not tq_in_range
}

tq_in_range if {
	qle(tq, _MAX_T)
	qle({"$q": [0 - qn(_MAX_T), 0]}, tq)
}

errs[6] := sprintf("time %v is before the previous observation (%v)", [obs.t, qf(S.t)]) if {
	not finishing
	is_number(obs.t)
	S.t != null
	qcmp(tq, S.t) < 0
}

errs[16] := _RANGE_ERR if {
	not finishing
	is_object(obs)
	walk(obs, [_, x])
	is_number(x)
	not dec(x)
} else := _RANGE_ERR if {
	not finishing
	first
	some src in [B, R]
	walk(src, [_, x])
	is_number(x)
	not dec(x)
}

_RANGE_ERR := "a number of the observation, its request or its bindings is outside the range of exact numbers (at most 400 significant digits, at most 400 decimals, an absolute value of at most 1.7976931348623157e308)"

errs[7] := sprintf("'%s' is allowed only on the first line", [key]) if {
	not finishing
	some key in ["bindings", "request"]
	has(obs, key)
	not first
} else := sprintf("'%s' must be an object", [key]) if {
	not finishing
	some key in ["bindings", "request"]
	has(obs, key)
	not is_object(obs[key])
}

errs[8] := "'values' must be an object" if {
	not finishing
	has(obs, "values")
	not is_object(obs.values)
}

errs[9] := "'messages' must be a list" if {
	not finishing
	has(obs, "messages")
	not is_array(obs.messages)
}

bad_message(m) if not is_object(m)

bad_message(m) if {
	some k, _ in m
	not k in _MSG_KEYS
}

bad_message(m) if {
	some k in ["from", "to", "name"]
	not is_string(m[k])
}

errs[10] := "a message is an object with from, to, name and optional args" if {
	not finishing
	is_array(obs.messages)
	some m in obs.messages
	bad_message(m)
} else := sprintf("'%s' of a message must be a concrete actor such as PLANNER or ROBOT[2], got %v", [k, m[k]]) if {
	not finishing
	is_array(obs.messages)
	some m in obs.messages
	some k in ["from", "to"]
	not regex.match(_ACTOR_RE, m[k])
} else := sprintf("'name' of a message must be a message name (an identifier), got %v", [m.name]) if {
	not finishing
	is_array(obs.messages)
	some m in obs.messages
	not regex.match(_NAME_RE, m.name)
} else := "'args' of a message must be a list" if {
	not finishing
	is_array(obs.messages)
	some m in obs.messages
	has(m, "args")
	not is_array(m.args)
}

errs[11] := "'state' must be a string" if {
	not finishing
	has(obs, "state")
	not is_string(obs.state)
}

errs[12] := "'end' must be true or false" if {
	not finishing
	has(obs, "end")
	obs.end != null
	not is_boolean(obs.end)
}

errs[13] := "'amend' (an amendment, F.6) is not supported by the norms monitor of the OPA target" if {
	not finishing
	has(obs, "amend")
	obs.amend != null
}

# what this monitor does not accept, unlike cadl norms-eval

bad_scalar(v) if is_array(v)

bad_scalar(v) if is_object(v)

has_array(v) if {
	walk(v, [_, x])
	is_array(x)
}

errs[14] := "a value of 'values' must be null, a boolean, a number or a string" if {
	not finishing
	not S.ended
	is_object(obs.values)
	some _, v in obs.values
	bad_scalar(v)
} else := "two keys of 'values' are the same name without whitespace" if {
	not finishing
	not S.ended
	is_object(obs.values)
	count({canon(k) | some k, _ in obs.values}) != count(obs.values)
} else := "a binding must be an integer or a string" if {
	not finishing
	not S.ended
	first
	some _, v in B
	not is_string(v)
	not int_number(v)
} else := "'request' must not contain lists" if {
	not finishing
	not S.ended
	first
	has_array(R)
} else := "'quantities' must be a list of names" if {
	has(input, "quantities")
	not is_array(input.quantities)
}

# an integer written as one: 1.0 and 1e2 are not (cadl norms-eval would
# write the member as A[1.0])
int_number(v) if {
	is_number(v)
	regex.match(`^-?(0|[1-9][0-9]*)$`, json.marshal(v))
}

errs[15] := sprintf("state '%s' at t=%v is not a state of the lifecycle of %s", [lc_after, obs.t, model.contract]) if {
	not finishing
	not S.ended
	lc != null
	not lc_after in {s | some s in lc.states}
}

error := errs[min(object.keys(errs))] if count(errs) > 0

result := {"error": error, "state": S} if {
	count(errs) > 0
} else := finish_result if {
	finishing
	not S.ended
} else := ended_result if {
	finishing
} else := ignored_result if {
	S.ended
} else := step_result
# <<< norms-monitor-library
