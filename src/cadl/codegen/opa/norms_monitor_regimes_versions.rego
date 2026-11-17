# >>> norms-monitor-regimes-versions
# Versions together with regime bindings (Appendix K, K.11, "Versions and
# regimes"; Appendix A, A.7.1; Appendix F, F.5 and F.6).
#
# This part is in the monitor generated with the other versions of the file
# when a version of the run has `regimes:` (`cadl codegen -t opa FILE
# --version OTHER ...`); `cadl codegen` copies it verbatim after the library
# (read with the changes of the regime part: the core runs on the clock of
# the contract) and norms_monitor_versions.rego (whose rule `result` it
# renames to `v_result`). The generated part defines `_RV`: the regimes of
# each version, those that do not have the contract in force, and the
# initial regime.
#
# An observation may name the current regime with the key "regime" and the
# version in force with the key "version". At an observation the regime
# becomes current first, then the versions take effect. Before the first
# "regime" the run is in the initial regime of the version in force at its
# first observation, and before the first "version" the first version is in
# force. A regime that is current at some observation must be a regime of
# every version in force at some observation (a version without `regimes:`
# has only the run in no regime). The contract is in force by the regime
# bindings of the version in force. While it is not in force the instance
# sees nothing and its clock stops, as in norms_monitor_regimes.rego; a
# version that takes effect then still reaches the instance (keep, switch, a
# waiting switch judged on the lifecycle state the instance saw last), and
# the amendment is applied at the next observation at which the contract is
# in force, or at which the instance ends, before anything else; a norm
# that it violates there is reported without reparation or transition. The
# instance starts at the first observation at which the contract is in
# force (or at which it ends), under the version in force there. An
# observation cannot also amend the contract ("amend").

_RVFORMAT := "cadl-norms-monitor-regimes-versions/1"

default _rv := null

_rv := input.state if input.state != null

rv_init := {
	"format": _RVFORMAT, "contract": models[0].contract, "v": null,
	"regime": null, "k": null, "used": [], "seen": [], "out_since": null, "offset": qz,
	"lines": 0, "last_t": null, "held": false, "held_t": null, "pend_values": {},
	"pend_state": null, "pend_bindings": null, "pend_request": null,
	"pend": null, "deferred": [],
}

RV := _rv if {
	_rv != null
} else := rv_init

rv_finish if input.finish == true

rv_obs := object.get(input, "observation", null)

# the observation as the versions and the core read it
rv_cobs := object.remove(rv_obs, ["regime", "version"]) if {
	is_object(rv_obs)
} else := rv_obs

rv_extra := object.remove(input, ["state", "observation", "finish"])

rv_has(o, key) if {
	is_object(o)
	key in object.keys(o)
}

# the instance has started (an observation of it has been judged)
rv_started if RV.v != null

rv_ended if {
	rv_started
	RV.v.core.ended
}

rv_wt := dec(rv_obs.t)

# --- the version and the regime of this observation -----------------------------------

rv_k := vindex(rv_obs.version) if {
	rv_has(rv_obs, "version")
} else := 0 if {
	RV.k == null
} else := RV.k

rv_initial(v) := _RV.versions[v].initial if {
	_RV.versions[v] != null
} else := null

rv_regime := rv_obs.regime if {
	rv_has(rv_obs, "regime")
} else := rv_initial(rv_k) if {
	RV.k == null
} else := RV.regime

rv_all_regimes := {r | some b in _RV.versions; b != null; some r in b.regimes}

rv_used := RV.used if {
	rv_k in RV.used
} else := array.concat(RV.used, [rv_k])

rv_seen := RV.seen if {
	rv_regime in RV.seen
} else := array.concat(RV.seen, [rv_regime])

# whether the run may be in regime r (null: in no regime) under version v
rv_of(v, r) if {
	r == null
	_RV.versions[v] == null
}

rv_of(v, r) if {
	r != null
	_RV.versions[v] != null
	r in {x | some x in _RV.versions[v].regimes}
}

rv_in_force if _RV.versions[rv_k] == null

rv_in_force if {
	_RV.versions[rv_k] != null
	not rv_regime in {x | some x in _RV.versions[rv_k].out}
}

# --- the clock of the contract --------------------------------------------------------------

rv_offset2 := qadd(RV.offset, qsub(rv_wt, RV.out_since)) if {
	rv_in_force
	RV.out_since != null
} else := RV.offset

rv_out_since2 := null if {
	rv_in_force
} else := RV.out_since if {
	RV.out_since != null
} else := rv_wt

rv_clock := qsub(rv_wt, rv_offset2) if {
	rv_in_force
} else := qsub(rv_out_since2, rv_offset2)

# --- the core state that the next step reads -------------------------------------------------

# the version whose contract the instance has (before the amendments of this
# observation): the one it follows, or the version in force before it starts
rv_ev := RV.v.follows if {
	rv_started
} else := rv_k

rv_lc := models[rv_ev].lifecycle

rv_base := RV.v.core if {
	rv_started
} else := x if {
	x := init_state with input.cv as rv_k
}

# the values and the lifecycle state given while the contract was not in
# force are seen now
rv_prepared := w_set(rv_base, {
	"values": object.union(rv_base.values, RV.pend_values),
	"known": sort([k | some k in ({x | some x in rv_base.known} | {x | some x, _ in RV.pend_values})]),
	"lc": rv_prep_lc,
})

rv_prep_lc := RV.pend_state if {
	RV.pend_state != null
} else := rv_base.lc

rv_prep_obs := w_set(rv_cobs, {k: v |
	some k, v in {"bindings": RV.pend_bindings, "request": RV.pend_request}
	v != null
}) if {
	not rv_started
} else := rv_cobs

# the lifecycle state after this observation, when the contract is not in force
rv_after := rv_cobs.state if {
	has(rv_cobs, "state")
} else := rv_prep_lc if {
	rv_prep_lc != null
} else := rv_lc.initial if {
	rv_lc != null
} else := null

rv_ends if rv_cobs.end == true

rv_ends if {
	rv_lc != null
	rv_after in {s | some s in rv_lc.terminal}
}

# --- the version the instance follows (norms_eval.VersionTracker), also while the
# contract is not in force -------------------------------------------------------------------

# the lifecycle state the instance sees before this observation: the state
# before it when the contract is in force, else the state after the last
# observation of the instance
rv_tstate := null if {
	rv_lc == null
} else := rv_prep_lc if {
	rv_in_force
	rv_prep_lc != null
} else := RV.v.core.lc if {
	not rv_in_force
	RV.v.core.lc != null
} else := rv_lc.initial

rv_in(v, st) if st in {s | some s in _VERSIONS.rules[v].states}

rv_switches(v, st) if {
	_VERSIONS.rules[v].rule != "keep"
	count(_VERSIONS.rules[v].states) == 0
}

rv_switches(v, st) if {
	_VERSIONS.rules[v].rule != "keep"
	rv_in(v, st)
}

rv_waits(v, st) if {
	_VERSIONS.rules[v].rule != "keep"
	count(_VERSIONS.rules[v].states) > 0
	not rv_in(v, st)
}

rv_p0(pend, st) := [pend] if {
	pend != null
	rv_in(pend, st)
} else := []

rv_pend0(pend, st) := null if {
	pend != null
	rv_in(pend, st)
} else := pend

rv_steps(cur, k) := [v | some v in numbers.range(cur + 1, k)] if {
	k > cur
} else := []

rv_pend_after(pend0, steps, st) := pend0 if {
	count(steps) == 0
} else := steps[count(steps) - 1] if {
	rv_waits(steps[count(steps) - 1], st)
} else := null

# a waiting switch whose state holds is made first, then the versions that
# take effect at this observation, in order
rv_walk(cur, pend, st, k) := {"path": array.concat(rv_p0(pend, st), [v | some v in steps; rv_switches(v, st)]), "pending": rv_pend_after(rv_pend0(pend, st), steps, st)} if {
	steps := rv_steps(cur, k)
}

rv_track := rv_walk(RV.k, RV.pend, rv_tstate, rv_k) if {
	rv_started
} else := {"path": [], "pending": null}

# the versions to amend to at the next observation of the instance
rv_deferred := array.concat(RV.deferred, rv_track.path)

rv_vpath := array.concat([RV.v.follows], rv_deferred) if {
	rv_started
} else := [rv_k]

rv_vstate := w_set(RV.v, {"core": rv_prepared}) if {
	rv_started
} else := null

# the input of the part for versions at this observation
rv_vin := w_set(rv_extra, {
	"state": rv_vstate, "observation": rv_prep_obs, "clock": rv_clock,
	"vk": rv_k, "vpath": rv_vpath, "vpending": rv_track.pending, "core0": rv_prepared,
})

# --- checks -----------------------------------------------------------------------------------

rverrs[0] := "the state was not made by the monitor of this contract" if {
	_rv != null
	not _rv.format == _RVFORMAT
}

rverrs[0] := "the state was not made by the monitor of this contract" if {
	_rv != null
	_rv.contract != models[0].contract
}

rverrs[1] := "the contract instance has no observation" if {
	rv_finish
	RV.lines == 0
}

rverrs[2] := sprintf("'regime' must name a regime of a version of the run (%s), got %s", [concat(", ", sort([r | some r in rv_all_regimes])), rv_got(rv_obs.regime)]) if {
	not rv_finish
	rv_has(rv_obs, "regime")
	not rv_obs.regime in rv_all_regimes
}

# also after the end of the instance, as cadl norms-eval and rverrs[2]
rverrs[3] := sprintf("'version' must name a version of the run (%s), got %s", [concat(", ", _VERSIONS.names), rv_got(rv_obs.version)]) if {
	not rv_finish
	rv_has(rv_obs, "version")
	not rv_obs.version in {v | some v in _VERSIONS.names}
}

rv_got(x) := sprintf("'%s'", [x]) if {
	is_string(x)
} else := "a value that is not a string"

rverrs[4] := sprintf("the trace names version %s, which comes before version %s, in force before (Appendix K, K.11)", [rv_obs.version, _VERSIONS.names[RV.k]]) if {
	not rv_finish
	not rv_ended
	rv_has(rv_obs, "version")
	RV.k != null
	vindex(rv_obs.version) < RV.k
}

rverrs[5] := "a trace that names the versions of a run with regime bindings cannot also amend the contract ('amend'), as cadl norms-eval" if {
	not rv_finish
	not rv_ended
	rv_has(rv_obs, "amend")
	rv_obs.amend != null
}

rverrs[6] := sprintf("'%s' is allowed only on the first line", [keys[0]]) if {
	not rv_finish
	RV.lines > 0
	keys := [key | some key in ["bindings", "request"]; rv_has(rv_obs, key)]
	count(keys) > 0
}

rverrs[7] := sprintf("time %v is before the previous observation (%v)", [rv_obs.t, qf(RV.last_t)]) if {
	not rv_finish
	is_object(rv_obs)
	is_number(rv_obs.t)
	RV.last_t != null
	qcmp(rv_wt, RV.last_t) < 0
}

# a regime of a run with versions is a regime of every version it uses (K.11)
rverrs[8] := sprintf("the run is in regime %s, which is not a regime of version %s; a regime of a run with versions must be a regime of every version that the run uses (Appendix K, K.11)", [rv_rname(bad[0][1]), _VERSIONS.names[bad[0][0]]]) if {
	not rv_finish
	not rv_ended
	bad := [[v, r] | some v in rv_used; some r in rv_seen; not rv_of(v, r)]
	count(bad) > 0
}

rv_rname(r) := "(none: a version without regimes:)" if {
	r == null
} else := r

# the checks of the core and of the versions (the form of the observation, the
# lifecycle state): at an observation not in force, by the contract the
# instance has (and, when the instance ends there, after the amendments that
# waited, see rv_out_end) ...
rverrs[9] := e if {
	not rv_finish
	not rv_ended
	not rv_in_force
	e := werror with input as w_set(rv_extra, {
		"state": rv_vstate, "observation": rv_cobs, "vk": rv_k, "vpath": [rv_ev],
	})
}

# ... and at an observation in force, after the amendments
rverrs[10] := e if {
	not rv_finish
	not rv_ended
	rv_in_force
	e := werror with input as w_set(rv_extra, {
		"state": rv_vstate, "observation": rv_prep_obs, "vk": rv_k, "vpath": rv_vpath,
		"vpending": rv_track.pending, "core0": rv_prepared,
	})
}

rverror := rverrs[min(object.keys(rverrs))] if count(rverrs) > 0

# --- the result -------------------------------------------------------------------------------

rv_fix_t(list, tt) := [w_set(e, {"t": tt}) | some e in list]

rv_trace_t(r, tt) := w_set(r, {
	"changes": rv_fix_t(r.changes, tt),
	"violations": rv_fix_t(r.violations, tt),
	"institutional_events": rv_fix_t(r.institutional_events, tt),
	"powers_exercised": rv_fix_t(r.powers_exercised, tt),
	"unauthorised": rv_fix_t(r.unauthorised, tt),
})

# the next deadline of the core (on the clock), as a time of the trace
rv_next_deadline(st, off, g) := qf(qadd(fd, off)) if {
	not st.ended
	dues := [due(st.norms[i], nd, st.t) |
		some i, nd in models[g].norms
		nd.deadline != null
		nd.kind in {"obligation", "prohibition"}
		st.norms[i].state == "active"
		qle(elapsed(st.norms[i], st.t), nd.deadline)
	]
	some fd in dues
	every d in dues {
		qle(fd, d)
	}
} else := null

rv_next := {
	"lines": RV.lines + 1, "last_t": rv_wt, "regime": rv_regime, "k": rv_k,
	"used": rv_used, "seen": rv_seen, "out_since": rv_out_since2, "offset": rv_offset2,
}

rv_cleared := {"held": false, "held_t": null, "pend_values": {}, "pend_state": null,
	"pend_bindings": null, "pend_request": null}

rv_verdicts_now := w_verdicts(w_marks(RV.v.core, RV.v.follows, RV.v.retired_norms, RV.v.retired_items, RV.v.item_uids)) if {
	rv_started
} else := {"norms": {}, "assumptions": [], "guarantees": [], "ok": true}

# after the end: the core counts the observation
rv_after_end := {"error": r0.error, "state": RV} if {
	r0 := v_result with input as w_set(rv_extra, {"state": RV.v, "observation": rv_cobs})
	r0.error
} else := w_set(r0, {"state": w_set(RV, {"v": r0.state, "lines": RV.lines + 1, "last_t": rv_wt})}) if {
	r0 := v_result with input as w_set(rv_extra, {"state": RV.v, "observation": rv_cobs})
}

# an observation in force: the amendments that waited, then the step of the
# versions and the core on the clock
rv_in_force_r := {"error": r0.error, "state": RV} if {
	r0 := v_result with input as rv_vin
	r0.error
} else := w_set(rv_trace_t(r0, qf(rv_wt)), {
	"state": w_set(RV, w_set(w_set(rv_next, rv_cleared), {"v": r0.state, "pend": rv_track.pending, "deferred": []})),
	"next_deadline": rv_next_deadline(r0.state.core, rv_offset2, r0.state.follows),
	"in_force": true,
}) if {
	r0 := v_result with input as rv_vin
}

# an observation not in force that does not end the instance
rv_left_out := {
	"state": w_set(RV, w_set(rv_next, {
		"held": true, "held_t": rv_wt,
		"pend_values": object.union(RV.pend_values, {canon(k): v | some k, v in object.get(rv_cobs, "values", {})}),
		"pend_state": rv_pend_state,
		"pend_bindings": rv_pend_b,
		"pend_request": rv_pend_r,
		"pend": rv_pend_kept,
		"deferred": rv_deferred_kept,
	})),
	"changes": [], "violations": [], "institutional_events": [], "powers_exercised": [],
	"unauthorised": [],
	"verdicts": rv_verdicts_now,
	"ended": false,
	"next_deadline": null,
	"in_force": false,
}

rv_pend_kept := rv_track.pending if {
	rv_started
} else := null

rv_deferred_kept := rv_deferred if {
	rv_started
} else := []

rv_pend_state := rv_cobs.state if {
	has(rv_cobs, "state")
} else := RV.pend_state

rv_pend_b := rv_cobs.bindings if {
	RV.lines == 0
	has(rv_cobs, "bindings")
} else := RV.pend_bindings

rv_pend_r := rv_cobs.request if {
	RV.lines == 0
	has(rv_cobs, "request")
} else := RV.pend_request

# the bindings and the request of an instance that starts and ends at an
# observation not in force
rv_fsb := {} if {
	rv_started
} else := {k: v |
	some k, v in {"bindings": rv_pend_b, "request": rv_pend_r}
	v != null
}

# the end of the instance at an observation not in force (F.5, item 6): the
# amendments that waited, then the settlement on the stopped clock
rv_settle_in := w_set(rv_extra, {
	"state": rv_vstate, "observation": rv_cobs, "clock": rv_clock,
	"vk": rv_k, "vpath": rv_vpath, "vpending": rv_track.pending, "core0": rv_prepared,
	"after": rv_after, "fsb": rv_fsb,
})

rv_out_end := {"error": e, "state": RV} if {
	e := werror with input as rv_settle_in
} else := w_set(rv_trace_t(r0, qf(rv_wt)), {
	"state": w_set(RV, w_set(w_set(rv_next, rv_cleared), {"v": r0.state, "pend": null, "deferred": []})),
	"next_deadline": null,
	"in_force": false,
}) if {
	r0 := v_settle with input as rv_settle_in
}

# {"finish": true} after an observation left out: the instance ends there,
# on the stopped clock
rv_held_after := RV.pend_state if {
	RV.pend_state != null
} else := rv_base.lc if {
	rv_base.lc != null
} else := rv_hlc.initial if {
	rv_hlc != null
} else := null

rv_hlc := models[RV.v.follows].lifecycle if {
	rv_started
} else := models[RV.k].lifecycle

rv_held_in := {
	"state": rv_vstate, "observation": {"t": qf(RV.last_t)},
	"clock": qsub(RV.out_since, RV.offset),
	"vk": RV.k, "vpath": rv_held_path, "vpending": RV.pend, "core0": rv_held_core,
	"after": rv_held_after, "fsb": rv_held_fsb,
}

rv_held_path := array.concat([RV.v.follows], RV.deferred) if {
	rv_started
} else := [RV.k]

rv_held_core := rv_prepared if {
	rv_started
} else := x if {
	b := init_state with input.cv as RV.k
	x := w_set(b, {
		"values": object.union(b.values, RV.pend_values),
		"known": sort([k | some k, _ in RV.pend_values]),
		"lc": RV.pend_state,
	})
}

rv_held_fsb := {} if {
	rv_started
} else := {k: v |
	some k, v in {"bindings": RV.pend_bindings, "request": RV.pend_request}
	v != null
}

rv_finish_r := w_set(r0, {"state": w_set(RV, {"v": r0.state})}) if {
	rv_ended
	r0 := v_result with input as {"state": RV.v, "finish": true}
} else := {"error": e, "state": RV} if {
	RV.held
	e := werror with input as rv_held_in
} else := w_set(rv_trace_t(r0, qf(RV.held_t)), {
	"state": w_set(RV, w_set(rv_cleared, {"v": r0.state, "pend": null, "deferred": []})),
	"next_deadline": null,
	"in_force": false,
}) if {
	RV.held
	r0 := v_settle with input as rv_held_in
} else := w_set(rv_trace_t(r0, qf(RV.last_t)), {
	"state": w_set(RV, {"v": r0.state}),
	"next_deadline": null,
	"in_force": true,
}) if {
	r0 := v_result with input as {"state": RV.v, "finish": true}
}

result := {"error": rverror, "state": RV} if {
	count(rverrs) > 0
} else := rv_finish_r if {
	rv_finish
} else := rv_after_end if {
	rv_ended
} else := rv_in_force_r if {
	rv_in_force
} else := rv_out_end if {
	rv_ends
} else := rv_left_out

# --- the settlement, in the part for versions (evaluated with the input above) ---------------

# what the amendments start from: the instance, or (at its first observation)
# the contract of the version in force
v_settle_ctx := {"uids": W.item_uids, "retired": W.retired_norms, "ritems": W.retired_items,
	"uid": W.uid, "before": w_before, "started": W.started} if {
	W.core != null
} else := {"uids": w_init_uids(w_k), "retired": [], "ritems": {"assume": [], "guarantee": []},
	"uid": 0, "before": b, "started": w_k} if {
	b := w_marks(init_state, w_k, [], {"assume": [], "guarantee": []}, w_init_uids(w_k)) with input.cv as w_k
}

# a norm violated by the amendment: reported without reparation or transition
v_amend_violation(rec, nd, tt) := {
	"t": qf(tt), "norm": nd.id, "severity": nd.severity, "verdict": rec.verdict,
	"sanction": nd.sanction, "reparation": nd.reparation_id,
	"reparation_activated": false, "transition": null,
}

v_settle := r if {
	cx := v_settle_ctx
	core0 := input.core0
	st0 := {"core": core0, "retired": cx.retired, "items": cx.ritems, "uids": cx.uids, "uid": cx.uid, "viol": []}
	st := w_chain(st0, w_path, input.clock, core0.n)
	g := w_path[count(w_path) - 1]
	fs := w_set(st.core, w_set(input.fsb, {"t": input.clock, "n": core0.n + 1, "lc": input.after}))
	r0 := core_result with input as {"state": fs, "finish": true, "cv": g}
	m := w_marks(r0.state, g, st.retired, st.items, st.uids)
	amv := [v_amend_violation(r0.state.norms[i], nd, input.clock) |
		some id in st.viol
		some i, nd in models[g].norms
		nd.id == id
	]
	r := w_set(r0, {
		"state": w_set(W, {"core": r0.state, "lines": W.lines + 1, "retired_norms": st.retired,
			"retired_items": st.items, "item_uids": st.uids, "current": w_k, "follows": g,
			"started": cx.started, "pending": null, "uid": st.uid}),
		"changes": w_changes(cx.before, m, qf(input.clock)),
		"violations": array.concat(amv, r0.violations),
		"verdicts": w_verdicts(m),
		"version": _VERSIONS.names[g],
	})
}
# <<< norms-monitor-regimes-versions
