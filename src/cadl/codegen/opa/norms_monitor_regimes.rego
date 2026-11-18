# >>> norms-monitor-regimes
# Regimes (Appendix A, A.7.1; Appendix F, F.5, "Contracts not in force").
#
# This part is in the monitor of a contract that some regime of the file
# does not have in force; `cadl codegen -t opa` copies it verbatim after
# the library, whose rule `result` it renames to `core_result`. The
# generated part defines `_REGIMES`: the regimes of the file, those that
# do not have the contract in force ("out") and the initial regime.
#
# An observation may name the current regime with the key "regime"; it
# stays current until another observation names one, and before the first
# one the run is in the initial regime. At an observation of a regime that
# does not have the contract in force the instance sees nothing: the
# result has no change, its values are seen from the next observation in
# force, its messages are not seen, and the clock of the contract stops.
# Deadlines and the intervals of the temporal operators run on that clock
# (the time of the trace without the time not in force); `now` and every
# time the result reports are times of the trace. An instance that ends at
# such an observation ("end": true, a terminal state, or {"finish": true}
# after it) is settled there, on the stopped clock. The state that the
# host stores keeps the clock (`offset`, `out_since`): the host passes
# nothing but the observations.

_RFORMAT := "cadl-norms-monitor-regimes/1"

default _w := null

_w := input.state if input.state != null

w_init := {
	"format": _RFORMAT, "contract": model.contract, "core": null,
	"regime": _REGIMES.initial, "out_since": null, "offset": qz, "lines": 0,
	"last_t": null, "held": false, "held_t": null, "pend_values": {}, "pend_state": null,
	"pend_bindings": null, "pend_request": null,
}

W := _w if {
	_w != null
} else := w_init

wfinish if input.finish == true

wobs := object.get(input, "observation", null)

# the observation as the core reads it: without "regime"
cobs := object.remove(wobs, ["regime"]) if {
	is_object(wobs)
} else := wobs

# what the core is passed besides the state and the observation
w_extra := object.remove(input, ["state", "observation", "finish", "clock"])

w_named if {
	is_object(wobs)
	has(wobs, "regime")
}

cur_regime := wobs.regime if {
	w_named
} else := W.regime

in_force if not cur_regime in {r | some r in _REGIMES.out}

w_in_force_v := true if {
	in_force
} else := false

wt := dec(wobs.t)

offset2 := qadd(W.offset, qsub(wt, W.out_since)) if {
	in_force
	W.out_since != null
} else := W.offset

out_since2 := null if {
	in_force
} else := W.out_since if {
	W.out_since != null
} else := wt

clock := qsub(wt, offset2) if {
	in_force
} else := qsub(out_since2, offset2)

# the core state that the next step of the core reads: the values and the
# lifecycle state given while the contract was not in force are seen now
w_base := W.core if {
	W.core != null
} else := init_state

prepared := w_set(w_base, {
	"values": object.union(w_base.values, W.pend_values),
	"known": sort([k | some k in ({x | some x in w_base.known} | {x | some x, _ in W.pend_values})]),
	"lc": prep_lc,
})

prep_lc := W.pend_state if {
	W.pend_state != null
} else := w_base.lc

prep_obs := w_set(cobs, {k: v |
	some k, v in {"bindings": W.pend_bindings, "request": W.pend_request}
	v != null
}) if {
	W.core == null
} else := cobs

# the lifecycle state after an observation at which the contract is not in force
w_after := cobs.state if {
	has(cobs, "state")
} else := prep_lc if {
	prep_lc != null
} else := lc.initial if {
	lc != null
} else := null

w_ends if cobs.end == true

w_ends if {
	lc != null
	w_after in {s | some s in lc.terminal}
}

# --- checks ---------------------------------------------------------------------

werrs[0] := "the state was not made by the monitor of this contract" if {
	_w != null
	not _w.format == _RFORMAT
}

werrs[0] := "the state was not made by the monitor of this contract" if {
	_w != null
	_w.contract != model.contract
}

werrs[1] := "the contract instance has no observation" if {
	wfinish
	W.lines == 0
}

werrs[2] := sprintf("'regime' must name a regime of the file (%s), got %s", [concat(", ", _REGIMES.regimes), w_got]) if {
	not wfinish
	w_named
	not wobs.regime in {r | some r in _REGIMES.regimes}
}

w_got := sprintf("'%s'", [wobs.regime]) if {
	is_string(wobs.regime)
} else := "a value that is not a string"

# the checks of the core (the form of the observation)
werrs[3] := e if {
	not wfinish
	e := error with input as w_set(w_extra, {"state": W.core, "observation": cobs})
}

werrs[4] := sprintf("'%s' is allowed only on the first line", [key]) if {
	not wfinish
	is_object(wobs)
	some key in ["bindings", "request"]
	has(wobs, key)
	W.lines > 0
}

werrs[5] := sprintf("time %v is before the previous observation (%v)", [wobs.t, qf(W.last_t)]) if {
	not wfinish
	is_number(wobs.t)
	W.last_t != null
	qcmp(wt, W.last_t) < 0
}

werror := werrs[min(object.keys(werrs))] if count(werrs) > 0

# --- the result -------------------------------------------------------------------

# object.union merges nested objects; w_set replaces the keys of `patch`
w_set(a, patch) := object.union(object.remove(a, object.keys(patch)), patch)

fix_t(list, tt) := [w_set(e, {"t": tt}) | some e in list]

with_trace_t(r, tt) := w_set(r, {
	"changes": fix_t(r.changes, tt),
	"violations": fix_t(r.violations, tt),
	"institutional_events": fix_t(r.institutional_events, tt),
	"powers_exercised": fix_t(r.powers_exercised, tt),
	"unauthorised": fix_t(r.unauthorised, tt),
})

# next_deadline of the core, on the clock, as a time of the trace
w_next_deadline(st, off) := qf(qadd(first, off)) if {
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

w_next := {
	"lines": W.lines + 1, "last_t": wt, "regime": cur_regime,
	"out_since": out_since2, "offset": offset2,
}

w_cleared := {"held": false, "held_t": null, "pend_values": {}, "pend_state": null,
	"pend_bindings": null, "pend_request": null}

# after the end: the core counts the observation
w_after_end := r if {
	r0 := core_result with input as w_set(w_extra, {"state": W.core, "observation": cobs})
	r := w_set(r0, {"state": w_set(W, w_set(w_next, {"core": r0.state})), "in_force": w_in_force_v})
}

# Every function below gets what it reads as arguments: OPA may reuse the
# value of a call with the same arguments within one query.

# an observation in force: the core step on the clock
w_in_force := {"error": r0.error, "state": W} if {
	r0 := core_result with input as w_set(w_extra, {"state": prepared, "observation": prep_obs, "clock": clock})
	r0.error
} else := w_set(with_trace_t(r0, qf(wt)), {
	"state": w_set(W, w_set(w_set(w_next, w_cleared), {"core": r0.state})),
	"next_deadline": w_next_deadline(r0.state, offset2),
	"in_force": true,
}) if {
	r0 := core_result with input as w_set(w_extra, {"state": prepared, "observation": prep_obs, "clock": clock})
}

# an observation not in force that does not end the instance
w_left_out := {
	"state": w_set(W, w_set(w_next, {
		"held": true, "held_t": wt,
		"pend_values": object.union(W.pend_values, {canon(k): v | some k, v in object.get(cobs, "values", {})}),
		"pend_state": w_after_state_kept,
		"pend_bindings": w_pend_b,
		"pend_request": w_pend_r,
	})),
	"changes": [], "violations": [], "institutional_events": [], "powers_exercised": [],
	"unauthorised": [],
	"verdicts": verdicts(w_base),
	"ended": false,
	"next_deadline": null,
	"in_force": false,
}

w_after_state_kept := cobs.state if {
	has(cobs, "state")
} else := W.pend_state

w_pend_b := cobs.bindings if {
	W.lines == 0
	has(cobs, "bindings")
} else := W.pend_bindings

w_pend_r := cobs.request if {
	W.lines == 0
	has(cobs, "request")
} else := W.pend_request

# the settlement on the stopped clock (F.5, item 6): the core ends the
# instance at a new observation at time `at` of the clock
w_settle_state(at, after_state, p, w) := w_set(p, {
	"t": at, "n": p.n + 1, "lc": after_state,
	"bindings": w_or(w.pend_bindings, p.bindings, w.core),
	"request": w_or(w.pend_request, p.request, w.core),
})

w_or(pending, current, core) := pending if {
	core == null
	pending != null
} else := current

w_settled(r0, tt, w) := {"error": r0.error, "state": w} if {
	r0.error
} else := w_set(with_trace_t(r0, qf(tt)), {
	"state": w_set(w, {"core": r0.state, "held": false}),
	"in_force": false,
})

w_out_end := w_set(r1, {"state": w_set(r1.state, w_set(w_next, w_cleared))}) if {
	fs := w_settle_state(clock, w_after, prepared, W)
	r0 := core_result with input as {"state": fs, "finish": true}
	r1 := w_settled(r0, wt, W)
}

# {"finish": true}
w_finish := w_set(r0, {"state": w_set(W, {"core": r0.state})}) if {
	W.core != null
	W.core.ended
	r0 := core_result with input as {"state": W.core, "finish": true}
} else := w_settled(r0, W.held_t, W) if {
	W.held
	fs := w_settle_state(qsub(W.out_since, W.offset), prep_lc, prepared, W)
	r0 := core_result with input as {"state": fs, "finish": true}
} else := w_set(w_settled(r0, W.last_t, W), {"next_deadline": null}) if {
	r0 := core_result with input as {"state": W.core, "finish": true}
}

result := {"error": werror, "state": W} if {
	count(werrs) > 0
} else := w_finish if {
	wfinish
} else := w_after_end if {
	W.core != null
	W.core.ended
} else := w_in_force if {
	in_force
} else := w_out_end if {
	w_ends
} else := w_left_out
# <<< norms-monitor-regimes
