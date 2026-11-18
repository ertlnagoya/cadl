# >>> norms-monitor-versions
# Versions of a run (Appendix K, K.11; Appendix F, F.6).
#
# This part is in the monitor generated with the other versions of the file
# (`cadl codegen -t opa FILE --version OTHER ...`); `cadl codegen` copies it
# verbatim after the library, whose rule `result` it renames to
# `core_result`. The generated part defines `models` (the contract in each
# version, in the order of the sequence by previous:) and `_VERSIONS` (the
# names of the versions and the transitional rule of the contract in each).
# The core runs the body of version `input.cv`.
#
# An observation may name the version in force with the key "version" (a
# version of the run; it cannot go back). The caller decides when a
# version takes effect (from the effective times, K.11) and names it at
# that observation; until a version is named, the first version is in
# force. The instance follows the version in force at its first
# observation. When a version takes effect, the instance follows the
# transitional rule of its contract in that version: keep leaves it as it
# is, switch amends it there (F.6), and switch with in_states amends it at
# the first observation whose lifecycle state before it is one of them; a
# waiting switch whose state holds is made first, and a waiting switch is
# dropped when a later version takes effect. The amendment is the one of
# F.6: a norm instance is carried over, transformed or discharged, a new
# norm without trigger is activated, and an assume: or guarantee: item of
# the same text continues. Norms and items that the new version no longer
# has stay in the result after those of the contract.

_VFORMAT := "cadl-norms-monitor-versions/1"

# object.union merges nested objects; w_set replaces the keys of `patch`
w_set(a, patch) := object.union(object.remove(a, object.keys(patch)), patch)

default _w := null

_w := input.state if input.state != null

w_init := {
	"format": _VFORMAT, "contract": models[0].contract, "core": null, "current": null,
	"follows": null, "pending": null, "started": null, "lines": 0,
	"retired_norms": [], "retired_items": {"assume": [], "guarantee": []},
	"uid": 0, "item_uids": {"assume": [], "guarantee": []}, "events": [],
}

W := _w if {
	_w != null
} else := w_init

wfinish if input.finish == true

wobs := object.get(input, "observation", null)

cobs := object.remove(wobs, ["version"]) if {
	is_object(wobs)
} else := wobs

w_extra := object.remove(input, ["state", "observation", "finish", "cv", "amend_viol"])

w_ended if {
	W.core != null
	W.core.ended
}

w_named if {
	is_object(wobs)
	has(wobs, "version")
	not w_ended
}

vindex(name) := i if {
	some i, v in _VERSIONS.names
	v == name
}

w_k := vindex(wobs.version) if {
	w_named
} else := 0 if {
	W.current == null
} else := W.current

# the version the core runs before this observation (the first one before the start)
w_cv := W.follows if {
	W.follows != null
} else := w_k

# --- checks ---------------------------------------------------------------------------

werrs[0] := "the state was not made by the monitor of this contract" if {
	_w != null
	not _w.format == _VFORMAT
}

werrs[0] := "the state was not made by the monitor of this contract" if {
	_w != null
	_w.contract != models[0].contract
}

werrs[1] := "the contract instance has no observation" if {
	wfinish
	W.core == null
}

# also after the end of the instance, as cadl norms-eval, which reads the
# key `version` of every line of the trace
werrs[2] := sprintf("'version' must name a version of the run (%s), got %s", [concat(", ", _VERSIONS.names), w_got]) if {
	not wfinish
	is_object(wobs)
	has(wobs, "version")
	not wobs.version in {v | some v in _VERSIONS.names}
}

w_got := sprintf("'%s'", [wobs.version]) if {
	is_string(wobs.version)
} else := "a value that is not a string"

werrs[3] := sprintf("the trace names version %s, which comes before version %s, in force before (Appendix K, K.11)", [wobs.version, _VERSIONS.names[W.current]]) if {
	not wfinish
	w_named
	W.current != null
	vindex(wobs.version) < W.current
}

werrs[4] := e if {
	not wfinish
	e := error with input as w_set(w_extra, {"state": W.core, "observation": cobs, "cv": w_pre_cv})
}

# the version whose lifecycle the state of the observation must be in
w_pre_cv := w_path[count(w_path) - 1] if {
	W.core != null
	not w_ended
} else := w_cv

# the state before this observation is not a state of the lifecycle of a
# version the instance switches to (F.6)
werrs[5] := sprintf("amendment: the contract instance is in state '%s', which is not a state of the lifecycle in version %s", [w_state, _VERSIONS.names[v]]) if {
	not wfinish
	W.core != null
	not w_ended
	w_state != null
	some v in array.slice(w_path, 1, count(w_path))
	models[v].lifecycle != null
	not w_state in {s | some s in models[v].lifecycle.states}
}

werror := werrs[min(object.keys(werrs))] if count(werrs) > 0

# --- the version the instance follows (norms_eval.VersionTracker) ---------------------------

# the lifecycle state before the observation
w_state := W.core.lc if {
	W.core.lc != null
} else := models[W.follows].lifecycle.initial if {
	models[W.follows].lifecycle != null
} else := null

w_in(v) if w_state in {s | some s in _VERSIONS.rules[v].states}

# a waiting switch whose state holds is made first
w_path0 := [W.follows, W.pending] if {
	W.pending != null
	w_in(W.pending)
} else := [W.follows]

w_pend0 := null if {
	W.pending != null
	w_in(W.pending)
} else := W.pending

# then the versions that take effect at this observation, in order
w_steps := [v | some v in numbers.range(W.current + 1, w_k)] if {
	w_k > W.current
} else := []

# each version that takes effect switches (switch, or in_states that hold),
# keeps, or leaves a waiting switch, which the next one drops
w_switches(v) if {
	_VERSIONS.rules[v].rule != "keep"
	count(_VERSIONS.rules[v].states) == 0
}

w_switches(v) if {
	_VERSIONS.rules[v].rule != "keep"
	w_in(v)
}

w_waits(v) if {
	_VERSIONS.rules[v].rule != "keep"
	count(_VERSIONS.rules[v].states) > 0
	not w_in(v)
}

w_walk := {
	"path": array.concat(w_path0, [v | some v in w_steps; w_switches(v)]),
	"pending": w_pend_after,
}

w_pend_after := w_pend0 if {
	count(w_steps) == 0
} else := w_steps[count(w_steps) - 1] if {
	w_waits(w_steps[count(w_steps) - 1])
} else := null

w_path := w_walk.path

# --- the amendment of F.6 (norms_eval._Engine._amend), from version a to b ----------------
# st: {"core": S, "retired": [records], "items": {...}, "uids": {...}, "uid": k, "viol": [ids]}

# the norms of the instance: those of the core, then the retired ones
w_pool(st, a) := array.concat([w_set(st.core.norms[i], {"id": nd.id, "dv": object.get(st.core.norms[i], "dv", W.started)}) |
	some i, nd in models[a].norms
], st.retired)

w_def(a, id) := nd if {
	some nd in models[a].norms
	nd.id == id
}

w_has(a, id) if {
	some nd in models[a].norms
	nd.id == id
}

w_elapsed(rec, tt) := elapsed(rec, tt)

w_late(rec, nd, tt) if {
	nd.kind in {"obligation", "prohibition"}
	nd.deadline != null
	count(rec.cands) == 0
	not qle(elapsed(rec, tt), nd.deadline)
}

# one old record: [kept (bool), the record in the new version, violated by the amendment]
w_carry(rec, a, b, tt, k, fa) := [false, null, false] if {
	rec.state == "inactive"
	not w_has(b, rec.id)
} else := [true, rec, false] if {
	rec.state in _FINAL
} else := [true, w_set(init_norm(0, w_def(b, rec.id)), {"from": 0, "dv": b}), false] if {
	rec.state == "inactive"
} else := [true, setst(rec, "discharged", tt, k, fa), false] if {
	w_def(a, rec.id).on_amendment in {"discharge", "transform"}
} else := [true, setst(rec, "discharged", tt, k, fa), false] if {
	not w_has(b, rec.id)
} else := [true, setst(rec, "discharged", tt, k, fa), false] if {
	w_def(b, rec.id).kind != w_def(a, rec.id).kind
} else := w_carry_late(w_set(w_carried(rec, a, b), {"dv": b}), w_def(b, rec.id), tt, k, fa)

w_carried(rec, a, b) := w_set(rec, {"cands": []}) if {
	w_def(a, rec.id).content_src != w_def(b, rec.id).content_src
} else := rec

w_carry_late(rec, nd, tt, k, fa) := [true, setst(rec, "violated", tt, k, fa), true] if {
	nd.kind == "obligation"
	rec.state in {"active", "suspended"}
	w_late(rec, nd, tt)
} else := [true, setst(rec, "fulfilled", tt, k, fa), false] if {
	nd.kind == "prohibition"
	rec.state in {"active", "suspended"}
	w_late(rec, nd, tt)
} else := [true, rec, false]

# the transforms: [target id, elapsed] of an old instance in force with on_amendment transform
w_transforms(pool, a, b, tt) := [[w_def(a, rec.id).transform_to, elapsed(rec, tt)] |
	some rec in pool
	rec.state in {"active", "suspended"}
	w_def(a, rec.id).on_amendment == "transform"
]

w_amend(st, a, b, tt, k) := out if {
	fa := st.core.failed_at
	pool := w_pool(st, a)
	ids := {rec.id | some rec in pool}
	carried := {rec.id: w_carry(rec, a, b, tt, k, fa) | some rec in pool}
	table := {id: c[1] | some id, c in carried; c[0]}
	newly := [id | some rec in pool; c := carried[rec.id]; c[2]; id := rec.id]
	# norms of the new version: carried, or new
	base := [w_new_rec(table, ids, nd, b) | some nd in models[b].norms]
	# transforms (F.6), then the new norms without trigger
	tf := w_transforms(pool, a, b, tt)
	after_tf := [w_transform_rec(base[i], nd, tf, tt, k, fa) | some i, nd in models[b].norms]
	norms := [w_added_rec(after_tf[i], nd, table, ids, tt, k, fa) | some i, nd in models[b].norms]
	retired := [table[rec.id] | some rec in pool; not w_has(b, rec.id); table[rec.id]]
	items := w_amend_items(st, a, b, k)
	out := {
		"core": w_set(st.core, {"norms": norms, "items": items.core}),
		"retired": retired,
		"items": items.retired,
		"uids": items.uids,
		"uid": items.uid,
		"viol": array.concat(st.viol, newly),
	}
}

w_new_rec(table, ids, nd, b) := w_set(table[nd.id], {"id": nd.id}) if {
	table[nd.id]
} else := w_set(init_norm(0, nd), {"dv": b})

w_transform_rec(rec, nd, tf, tt, k, fa) := w_set(setst(rec, "active", tt, k, fa), {"before": w_tf_before(nd, e)}) if {
	hits := [x | some x in tf; x[0] == nd.id]
	count(hits) > 0
	e := hits[0][1]
	rec.state == "inactive"
} else := rec

w_tf_before(nd, e) := e if {
	nd.kind in {"obligation", "prohibition"}
} else := qz

w_added_rec(rec, nd, table, ids, tt, k, fa) := setst(rec, "active", tt, k, fa) if {
	not table[nd.id]
	not nd.id in ids
	rec.state == "inactive"
	nd.trigger == null
	not nd.target
} else := rec

# --- items (norms_eval._Engine._amend_items) ---------------------------------------------

w_amend_items(st, a, b, k) := out if {
	parts := {key: w_items_key(st, a, b, k, key) | some key in ["assume", "guarantee"]}
	out := {
		"core": {key: parts[key].core | some key in ["assume", "guarantee"]},
		"retired": {key: parts[key].retired | some key in ["assume", "guarantee"]},
		"uids": {key: parts[key].uids | some key in ["assume", "guarantee"]},
		"uid": st.uid + parts.assume.added + parts.guarantee.added + 0,
	}
}

# the pool: the items of the core with their texts and uids, then the retired ones
w_item_pool(st, a, key) := array.concat(
	[w_set(st.core.items[key][i], {"text": md.text, "error": md.error != null, "uid": st.uids[key][i]}) |
		some i, md in models[a].items[key]
	],
	st.items[key],
)

w_items_key(st, a, b, k, key) := out if {
	pool := w_item_pool(st, a, key)
	news := models[b].items[key]
	# the first open item of the pool with the same text, for each new item,
	# taken in order (an item is taken once)
	match := {j: q |
		some j, nw in news
		r := count([x | some x, y in news; x < j; y.text == nw.text])
		cands := sort([q2 | some q2, it in pool; it.text == nw.text; not it.closed == true])
		r < count(cands)
		q := cands[r]
	}
	core := [w_new_item(pool, match, j, st.uid, key, k) | some j, _ in news]
	uids := [w_new_uid(pool, match, j, st.uid, key) | some j, _ in news]
	used := {p | some _, p in match}
	retired := [w_close(it) | some p, it in pool; not p in used]
	out := {"core": [object.remove(it, ["text", "error", "uid"]) | some it in core], "retired": retired, "uids": uids,
		"added": count([j | some j, _ in news; not match[j]])}
}

w_new_item(pool, match, j, uid, key, k) := pool[match[j]] if {
	match[j]
} else := w_set(init_item, {"start": k})

w_new_uid(pool, match, j, uid, key) := pool[match[j]].uid if {
	match[j]
} else := sprintf("%s~%d", [key, uid + count([x | some x in numbers.range(0, j); not match[x]])])

# an item that the new version does not have: closed when it is not decided
w_close(it) := w_set(it, {"closed": true, "value": null}) if {
	not it.closed == true
	not it.error
	it.value in {null, "undefined"}
} else := it

# --- marks and changes (as the norms monitor of the python target) --------------------------

assume_mark(it) := assume_status(it)

w_marks(core, cv, retired, ritems, uids) := {
	"norms": array.concat([{"id": nd.id, "state": core.norms[i].state, "verdict": core.norms[i].verdict} | some i, nd in models[cv].norms],
		[{"id": r.id, "state": r.state, "verdict": r.verdict} | some r in retired]),
	"assume": array.concat([{"uid": uids.assume[i], "text": md.text, "mark": assume_status(core.items.assume[i])} | some i, md in models[cv].items.assume],
		[{"uid": it.uid, "text": it.text, "mark": assume_status(it)} | some it in ritems.assume]),
	"guarantee": array.concat([{"uid": uids.guarantee[i], "text": md.text, "mark": core.items.guarantee[i].verdict, "monitored": md.unsupported == false} | some i, md in models[cv].items.guarantee],
		[{"uid": it.uid, "text": it.text, "mark": it.verdict, "monitored": true} | some it in ritems.guarantee]),
}

w_changes(before, after, tt) := array.concat(array.concat(array.concat(
	[{"t": tt, "kind": "norm", "id": x.id, "state": x.state, "verdict": x.verdict,
		"previous_state": w_prev_norm(before, x.id)[0], "previous_verdict": w_prev_norm(before, x.id)[1]} |
		some x in after.norms
		w_prev_norm(before, x.id) != [x.state, x.verdict]
	],
	[{"t": tt, "kind": "norm", "id": x.id, "state": null, "verdict": null,
		"previous_state": x.state, "previous_verdict": x.verdict} |
		some x in before.norms
		not x.id in {y.id | some y in after.norms}
	]),
	[{"t": tt, "kind": "assumption", "index": i + 1, "text": x.text, "status": x.mark,
		"previous": w_prev_item(before.assume, x.uid)} |
		some i, x in after.assume
		w_prev_item(before.assume, x.uid) != x.mark
	]),
	[{"t": tt, "kind": "guarantee", "index": i + 1, "text": x.text, "verdict": x.mark,
		"previous": w_prev_item(before.guarantee, x.uid)} |
		some i, x in after.guarantee
		w_prev_item(before.guarantee, x.uid) != x.mark
	])

w_prev_norm(before, id) := [x.state, x.verdict] if {
	some x in before.norms
	x.id == id
} else := [null, null]

w_prev_item(list, uid) := x.mark if {
	some x in list
	x.uid == uid
} else := "pending"

w_verdicts(m) := {
	"norms": {x.id: {"state": x.state, "verdict": x.verdict} | some x in m.norms},
	"assumptions": [{"index": i + 1, "text": x.text, "status": x.mark} | some i, x in m.assume],
	"guarantees": [{"index": i + 1, "text": x.text, "verdict": x.mark, "monitored": x.monitored} | some i, x in m.guarantee],
	"ok": w_ok(m),
}

w_ok(m) := false if {
	some x in m.norms
	x.verdict == "violated"
} else := false if {
	some x in m.guarantee
	x.mark == "violated"
} else := true

w_init_uids(cv) := {key: [sprintf("%s%d", [key, i]) | some i, _ in models[cv].items[key]] | some key in ["assume", "guarantee"]}

# --- the result -------------------------------------------------------------------------------

# the first observation: the core of the version in force
w_first := r if {
	r0 := core_result with input as w_set(w_extra, {"state": null, "observation": cobs, "cv": w_k})
	before := w_marks(init_state, w_k, [], {"assume": [], "guarantee": []}, w_init_uids(w_k)) with input.cv as w_k
	r := w_after(r0, w_k, w_init_uids(w_k), [], {"assume": [], "guarantee": []}, null, before,
		{"current": w_k, "follows": w_k, "started": w_k, "pending": null, "uid": 0})
}

w_after(r0, cv, uids, retired, ritems, pend, before, patch) := {"error": r0.error, "state": W} if {
	r0.error
} else := r if {
	m := w_marks(r0.state, cv, retired, ritems, uids)
	r := w_set(r0, {
		"state": w_set(W, w_set({"core": r0.state, "lines": W.lines + 1, "retired_norms": retired, "retired_items": ritems,
			"item_uids": uids}, patch)),
		"changes": w_changes(before, m, w_t),
		"verdicts": w_verdicts(m),
		"version": _VERSIONS.names[cv],
	})
}

w_t := qf(dec(wobs.t)) if {
	not wfinish
} else := qf(W.core.t)

w_before := w_marks(W.core, W.follows, W.retired_norms, W.retired_items, W.item_uids)

# a later observation: the amendments, then the core of the version the instance follows
w_step := r if {
	st0 := {"core": W.core, "retired": W.retired_norms, "items": W.retired_items, "uids": W.item_uids, "uid": W.uid, "viol": []}
	st := w_chain(st0, w_path, dec(wobs.t), W.core.n)
	g := w_path[count(w_path) - 1]
	r0 := core_result with input as w_set(w_extra, {"state": st.core, "observation": cobs, "cv": g, "amend_viol": st.viol,
		"power_seq": w_power_seq(st.core, st.retired, g)})
	r := w_after(r0, g, st.uids, st.retired, st.items, w_walk.pending, w_before,
		{"current": w_k_now, "follows": g, "pending": w_walk.pending, "uid": st.uid})
}

# the powers of the instance in the order of its norms, each with the
# definition it has: of the version the core runs ("cur") or of another one
w_power_seq(core, retired, g) := array.concat(
	[e | some i, nd in models[g].norms; e := w_entry(core.norms[i], nd, i, g)],
	[e | some rec in retired; e := w_ghost(rec, g)],
)

w_entry(rec, nd, i, g) := {"cur": i} if {
	object.get(rec, "dv", W.started) == g
	nd.kind == "power"
} else := w_ghost(w_set(rec, {"id": nd.id}), g) if {
	object.get(rec, "dv", W.started) != g
}

w_ghost(rec, g) := {"ghost": j} if {
	some j, gh in models[g].ghosts
	gh.u == object.get(rec, "dv", W.started)
	gh.id == rec.id
}

w_k_now := w_k if {
	w_k > W.current
} else := W.current

# one amendment of the chain, when the path has it
w_amend_at(st, path, i, tt, k) := st if {
	i >= count(path)
} else := w_amend(st, path[i - 1], path[i], tt, k)

# after the end: the core counts the observation
w_after_end := r if {
	r0 := core_result with input as w_set(w_extra, {"state": W.core, "observation": cobs, "cv": W.follows})
	m := w_marks(W.core, W.follows, W.retired_norms, W.retired_items, W.item_uids)
	r := w_set(r0, {"state": w_set(W, {"core": r0.state, "lines": W.lines + 1}), "verdicts": w_verdicts(m)})
}

w_finish := r if {
	r0 := core_result with input as {"state": W.core, "finish": true, "cv": W.follows}
	m := w_marks(r0.state, W.follows, W.retired_norms, W.retired_items, W.item_uids)
	r := w_set(r0, {"state": w_set(W, {"core": r0.state}), "changes": w_changes(w_before, m, w_t),
		"verdicts": w_verdicts(m), "version": _VERSIONS.names[W.follows]})
}

result := {"error": werror, "state": W} if {
	count(werrs) > 0
} else := w_finish if {
	wfinish
} else := w_after_end if {
	w_ended
} else := w_first if {
	W.core == null
} else := w_step
# <<< norms-monitor-versions
