# Changelog

All notable changes to the CADL language and toolchain are documented in
this file. The format follows [Keep a Changelog](https://keepachangelog.com/)
and the project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `cadl model-check FILE --regime-map ID --certify`: the **certified
  regime map** of a regime map query (Appendix H, H.9). The result is a
  set of cells that cover the region, each `decided` with the regimes
  that are optimal at every valuation of the cell, `infeasible`, or
  `undecided`, with the undecided fraction and whether `resolution:` is
  reached. The method is parameter lifting with exact rational arithmetic
  (new module `cadl.stochastic_regions`, no new dependency): on a cell a
  design is relaxed to a Markov decision process over the corners of the
  cell, whose minimal and maximal values bound the design's value on the
  whole cell; undecided cells are bisected, the largest first.
  `--max-depth N` (default 24) and `--max-cells N` (default 20000) bound
  the refinement; a map that stops at a budget is printed and says that
  the resolution is not reached. Exit status 0 when the resolution is
  reached, 1 when it is not, 2 on an error. `--format json` gives the
  cells, counts, fractions, method and budget under `regime_map`.
  Probabilities that are not multi-affine in the open parameters are
  enclosed by interval arithmetic; cells that are not graph-preserving
  are analysed soundly. `--grid N` still prints estimates at sample
  points, separately; `--grid` and `--certify` cannot be combined.
- Tests: every `decided` and `infeasible` cell of eight maps is checked
  at sample points with the exact point evaluator, and, when `stormpy`
  is installed, the bounds are compared with Storm's parameter lifting
  on the exported files. New test model
  `tests/data/stochastic_regions.cadl`.

### Fixed
- `cadl model-export`: a transition reward whose action label no command
  of the design has (a command with `when:` in another regime) is no
  longer written. PRISM 4.11 and Storm reject a reward item that names an
  action the model does not have, so such a file did not load. A reward
  structure left without items gets the item `true : (0);`, because Storm
  rejects an empty structure.

## [0.3.21] — 2026-11-11

Fixes found by running the generated files with the tools they are
written for: OPA 1.22.3, the Cedar CLI 4.14.1, PRISM 4.11 and Storm
(stormpy 2.0.0).

### Fixed
- `cadl codegen -t agent-rego`: a reference `A[i]` whose index the
  contract instance does not bind is unknown again, as G.4 requires. The
  policy of 0.3.20 tested `not is_number(bindings[ref.var])`, which OPA
  leaves undefined rather than true when the entry is missing, so the
  reference counted as no match. A prohibition with such a bearer did not
  apply; in a contract with `default: allow` the message was allowed where
  `cadl agents-decide` denies it, and elsewhere the result carried the
  reason `default` instead of `prohibition`. Regenerate policies written
  by 0.3.20. The lookups under `not` now use `object.get`.
- `cadl model-export`: a label or a reward structure named like a keyword
  of the PRISM language or one of its built-in functions (`max`, `min`,
  `module`, `floor`, ...) gets one trailing underscore, like every other
  name, in the model and in the properties. PRISM and Storm reject
  `label "max"` and `rewards "max"` although the name is quoted, so the
  files of 0.3.20 did not load. Two labels, or two reward structures,
  that are written alike after renaming are an export error.

### Added
- Tests that run the generated files when the tool is installed, and are
  skipped otherwise: the Cedar policies under `cedar authorize` against
  the reference decision function; the exported models under PRISM
  (`prism` on PATH) and under Storm (`stormpy` importable) against the
  reference evaluator, in exact rational arithmetic where the tool has
  it. `tests/data/stochastic_features.cadl` is a second model for them
  with `when:`, three-way updates, guarded state and transition rewards,
  `U`, `U<=k` and keyword names.
- A test without OPA for the first fix: no rule of the generated Rego has
  a lookup under `not`. The comparisons with OPA are skipped where the
  `opa` binary is absent, as in CI, which is how the defect went unnoticed.

## [0.3.20] — 2026-11-11

Verdicts and amendments of the norms extension on a recorded trace
(Appendix F), the agent governance extension (agents 0.1, Appendix G),
and the stochastic model extension (stochastic 0.1, Appendix H).

### Added
- `cadl norms-eval FILE --trace TRACE [--contract ID] [--format text|json]`:
  an offline verdict engine for the norms extension (Appendix F, F.4 and
  F.5). For one contract instance recorded as a timed trace (JSON Lines:
  `t`, `bindings`, `request`, `values`, `messages`, `state`, `end`), it
  computes the state of every norm instance over time and its verdict
  (`pending`, `fulfilled`, `violated`, `excused`), the reported violations
  with severity, sanction, activated reparation and requested lifecycle
  transition, the institutional events of `counts_as:` rules, unauthorised
  acts, and the verdicts of `assume:` and `guarantee:` items, including
  those with `within`, `throughout` and `until`. The lifecycle state is
  taken from the trace; a requested transition is reported, not applied.
  Exit status 0 when nothing is `violated`, 1 otherwise, 2 on a usage
  error. The README lists the trace format, the JSON output and the
  readings chosen where the specification leaves a choice.
- Amendments (Appendix F, F.6) in `cadl norms-eval`: a trace line with
  `"amend": "<new version of the file>"` replaces the contract at that
  observation. Active and suspended norm instances follow the
  `on_amendment:` of the old version (`carry_over`, `discharge`,
  `transform:`), inactive ones take the new definition or are removed, new
  norms start, and the `counts_as:` rules and `lifecycle:` of the new
  version apply from then on. The effects are in the timeline and under
  `amendments[]` in the JSON output.
- `cadl norms-diff OLD NEW [--contract ID] [--format text|json]`: what an
  amendment from one version to the next does to the norms of each
  contract: norms removed, added or of a changed kind, norms that carry
  over with a changed `content` or `deadline:`, `transform:` targets the
  new version lacks (an error), and changed `counts_as:` rules. Exit
  status 0 when nothing prevents the amendment, 1 on an error, 2 on a
  usage error.
- `examples/traces/`: five traces for `examples/norms_robot_delivery.cadl`,
  and `examples/norms_amendment_v1.cadl` / `norms_amendment_v2.cadl` with
  a trace that is amended from the first to the second.
- Agent governance extension, agents 0.1 (Appendix G of the specification),
  which builds on norms 0.1. An actor may have `agent:` (`kind:` of
  `llm_agent`, `tool_server`, `human` or `service`, `protocol:`, and for a
  tool server `tools:`); a contract may have `enforcement:` (`default:`,
  `point:`; a null or empty value means enforced with all defaults) and
  `delegations:` (`norm:`, `to:`, `grant:`, `revoke:`, `max_depth:`,
  `until:`); a prohibition may have `on_denied:` (`escalate_to:`,
  `timeout:`, `fallback:`, `notify:`). In a file that declares
  `agents: 0.1` together with `norms: 0.1`, `cadl check` applies rules G-1
  to G-16 of G.9: the SHOULDs (G-14, G-15, G-16, the protocol steps of G-4
  and parts of G-7, G-8, G-9 and G-12) are warnings, the rest errors.
  Without the declaration the four keys get one warning as undeclared
  extension keys and are neither checked nor lowered to the IR.
- `cadl agents-decide <file.cadl> --input <input.json> [--format text|json]`
  and `cadl.agents.decide`: the reference decision function of G.5 over
  the policy input of G.10 (`message`, optional `now`, optional `instances`
  with `bindings`, `in_force` and `derived`). It returns `decision`,
  `reason`, `contracts`, `norms`, `provenance`, `escalate_to`, `timeout`,
  `fallback` and `notify`; a message to a tool server that is a party
  of an enforced contract is governed whatever its sender; matching is
  three-valued and an unknown is resolved fail-closed. Exit status 0 for allow, 1 for deny, 3 for
  escalate, 2 for a usage error. The command is stateless: holds, release
  and expiry (G.6), grants, chains and revocations (G.7) and audit records
  (G.8) are not implemented, and the in-force flags of the norms that are
  not static and the valid derived instances come in through the input.
- `cadl codegen -t agent-rego` writes `agent_policy.rego`: one Rego package
  (`cadl.agent_policy`) that takes the policy input of G.10 as `input` and
  returns the result object, for static and non-static norms alike. It is
  independent of the `opa` target, whose output is unchanged.
- `cadl codegen -t agent-cedar` writes `agent_policy.cedar` and
  `agent_policy_table.json`: Cedar policies for the statically decidable
  part (forbid for undeclared tools and static prohibitions, permit for
  static permissions, default deny for tool calls), the side table from
  which a runtime computes the reason, the norms and the escalation data,
  and a README comment that states what is not covered (norms that are not
  static, derived permissions, contract instances). Both targets report an
  error for a file that does not declare the extension.
- `cadl sim-ir` lowers the new blocks (G.13): `agent_kind`,
  `agent_protocol` and `agent_tools` of an actor; `enforced`,
  `enforcement_default`, `enforcement_point` and `delegations` of a
  contract; `on_denied_escalate_to`, `on_denied_timeout_ms`,
  `on_denied_fallback` and `on_denied_notify` of a norm. These fields are
  part of the IR only for a file that declares the extension; the IR of
  every other file is unchanged.
- Example `examples/agents_tool_governance.cadl` (the example of G.12) with
  the sample calls of the appendix in `examples/agents_inputs/`.
- Stochastic model extension, stochastic 0.1 (Appendix H of the
  specification). In a file that declares `stochastic: 0.1` under
  `extensions:`, the `model:` block of an SoS is read: `kind`,
  `parameters` (free, or bound with `from:` to `alpha`, `beta`, `lambda`
  of a contract or to an entry of `context.environment`), `variables`,
  `commands` (guarded, with probabilistic `updates`), `labels`, `regimes`,
  `rewards`, `properties`, and `regime_maps`. The three expression
  sub-languages of H.2 (`state_pred`, `param_expr`, `property`) have
  parsers of their own in `cadl.stochastic`; the expression grammar of
  A.10 is unchanged.
- `cadl check` applies rules S-1 to S-15 of H.10 (the SHOULDs as
  warnings, S-15 as informational diagnostics). Numbers are exact: a YAML
  number is read as its shortest round-trip decimal. S-7 and S-9 are
  decided by enumerating the full state space; a model with more than
  200 000 states is refused with an error that names the limit. Names
  that a YAML 1.1 loader reads as booleans or null (`ON`, `No`, `Yes`,
  ...) are reported where they are mapping keys or `init:` values.
- The IR has a top-level field `stochastic_model` (H.13): the interpreted
  block, or `null`.
- `cadl model-export <file> [--output DIR] [--regime NAME]` writes, per
  design, a PRISM model file `<sos name>_<design>.prism` and a properties
  file `<sos name>_<design>.props` by the rules of H.11. A name equal to
  a PRISM keyword or built-in function name gets a trailing underscore;
  a name collision after that is an error.
- `cadl model-check <file> [--regime NAME] [--set p=v ...]
  [--format text|json]`: an internal reference evaluator for checking and
  tests, not a model checker. For a design with every parameter fixed it
  builds the Markov chain by enumeration and evaluates the properties of
  `model.properties:` in exact rational arithmetic (`P` with `F`,
  `F<=k`, `U`, `U<=k`; `R{"r"}` with `F`, including the value +inf).
  `--regime-map ID --grid N` evaluates a regime map query at the corner
  points of a uniform grid and prints the best regime at each point,
  labelled as estimates at sample points and not a certified regime map.
  Exit status: 0 when every threshold property evaluated is satisfied,
  1 when at least one is violated, 2 when nothing could be evaluated.
- `examples/stochastic_robot_delivery.cadl`, the example of H.12.

### Changed
- `cadl sim-ir` output has the new key `stochastic_model`, `null` for a
  file without an interpreted `model:` block.
- Without the declaration `stochastic: 0.1`, a `model:` block gets one
  warning as an undeclared extension key and is not checked, lowered to
  the IR, or exported.

## [0.3.19] — 2026-11-10

The norms extension (norms 0.1, Appendix F of the specification).

### Added
- Norms extension, norms 0.1 (Appendix F of the specification). A
  contract may have `norms:` (obligations, permissions, prohibitions and
  powers with a bearer, trigger, until, content, deadline, `on_violation:`,
  `effects:` and `on_amendment:`) and `counts_as:` rules (`when:`,
  `creates:`, `if:`). In a file that declares `norms: 0.1` under
  `extensions:`, `within(d, p)`, `throughout(d, p)` and `until(p, q, d)`
  are bounded temporal operators; the expression grammar is unchanged.
  `cadl check` applies rules N-1 to N-12 of F.7: N-8, N-12 and the SHOULDs
  of N-3 and N-9 are warnings, the rest errors. A temporal operator is an
  error (N-10) outside `assume:`, `guarantee:` and the `content:` of a
  norm, for example in a monitor `rule:`, a lifecycle `on:` / `when:`, a
  norm `trigger:` / `until:`, a `counts_as:` rule, a `condition:` or a
  metric `formula:`. Verdicts and amendments (F.5, F.6) are not computed.
- `cadl verify` leaves `assume:` and `guarantee:` items with a temporal
  operator out of the SMT checks when `norms: 0.1` is declared, and
  reports each one as not checked (`not_supported`, shown as `[SKIP]`),
  which does not fail verification. When this leaves a contract without
  assumptions (or without assumes or guarantees), the assumption and
  entailment lines say that no state-predicate items are left after the
  temporal items were left out, instead of "No assumptions (the contract
  applies unconditionally)".
- `cadl codegen` (python, solidity, opa, unity-csharp) leaves `assume:`
  and `guarantee:` items with a temporal operator out of the generated
  checks when `norms: 0.1` is declared, writes a comment in their place
  ("not generated: the item has a temporal operator (norms 0.1)"), and
  prints one warning per left-out item. Before, the Solidity and OPA
  targets emitted calls to `within` / `throughout`, which are not defined
  there. A Rego rule left without items is `true`. Without the
  declaration, generation is unchanged.
- The simulator IR has two more fields on every contract, `norms` and
  `counts_as`, in the flattened form of F.9 (`on_violation_*`,
  `effects_*`, `on_amendment`, `transform_to`; `when`, `creates`, `if`).
  They are empty lists for a contract without norms, so `cadl sim-ir`
  output of every file gains `"norms": []` and `"counts_as": []`.
- `examples/norms_robot_delivery.cadl`, the example of F.8: the delivery
  contract with an obligation and its contrary-to-duty obligation, a
  prohibition, a permission, a power, and a counts-as rule.

### Changed
- `cadl check` no longer gives the informational "extension not known"
  diagnostic for `norms: 0.1`; another version of `norms` gets the same
  information as other unknown versions. Without the declaration,
  `norms:` and `counts_as:` get one warning per contract as undeclared
  extension keys and are neither checked nor lowered to the IR (F.11);
  `within`, `throughout` and `until` remain ordinary function names.

## [0.3.18] — 2026-10-20

### Added
- Expressions accept a unary minus (`permit_state == -2`, `x > -0.5`,
  `-a * b`), as Appendix A, A.10 of the specification now allows. It binds
  tighter than `*` and `/`. A negative number is read as a literal; any
  other operand `x` is read as `0 - x`, so the verifier and the Python,
  Solidity, OPA and Unity C# generators handle it without change. Up to
  0.3.17 such an item was kept as text: excluded from verification, and
  reported as a warning when it was a lifecycle `on:` / `when:` or a
  monitor `rule:`.
- The predicate evaluator of the generated Unity C# runtime
  (`PredicateEvaluator.cs`) reads a minus sign in front of a number. A
  minus right after an operand (`x -1`) is a subtraction, which the
  evaluator does not support; such an expression is false, as before.
  The generated files keep their line counts.

### Changed
- `examples/raspimouse_c_sos.cadl`, `raspimouse_d_sos.cadl` and
  `raspimouse_mcp_sos.cadl`: triggers, conditions and an assumption that
  were written as prose are written as expressions of Appendix A, A.10 of
  the specification, so that tools read them instead of keeping them as
  text (for example `ROBOT[i] enters intersection node` →
  `entered_intersection(ROBOT[i])`, `goal_sequence.has_next()` →
  `has_next(goal_sequence)`, `edge(src, dst).occupied` →
  `occupied(edge(src, dst))`). CADL Explorer reported 13 of them as "not a
  valid expression". `cadl verify` results are unchanged (13/13, 8/8,
  12/12); `cadl sim-gen` output differs only in these strings.
- `examples/iot_data_sharing.cadl`: `retention_period <= 30d` is written
  `<= 720h`, since durations have the units `ms`, `s`, `min` and `h`; the
  item was kept as text and excluded from verification.

## [0.3.17] — 2026-10-19

### Changed
- `examples/sos_dsl_robot_delivery.cadl`: every message the example sends is
  declared by the interfaces of its sender and receiver. `ack`, the event of
  the `accept` transition, is added to the outputs of ROBOT and the inputs of
  DISPATCHER, and a protocol `DeliveryOrder` names the messages of one order
  (`delivery_request`, `route_assignment`, `ack`, `task_completion`,
  `delivery_notification`), so that CUSTOMER sends and receives them too.
  The lifecycle and monitors are unchanged, and so is the generated Unity C#.
  CADL Explorer 0.5.5 / 0.5.6 reported the missing `ack` and a CUSTOMER that
  exchanged no message.
- `examples/a_sos_robot_delivery.cadl`, `c_sos_taxi_fleet.cadl`,
  `iot_data_sharing.cadl` and `raspimouse_mcp_sos.cadl`: the interfaces
  declare every message the protocols and information sharing send
  (DISPATCHER's `delivery_request` / `delivery_notification`; TAXI's and
  PASSENGER's `ride_acceptance`, TAXI's `conflict_notification`,
  `incident_report` and `traffic_summary`; GATEWAY's `recommended_action` /
  `anomaly_report`; ROBOT's `occupy_request`, `release_request`,
  `availability_query` and `occupy_result`). Only interface lists change.
- README (English and Japanese): the `cadl ai` steps describe `--verify`.
- `examples/robot_delivery.cadl`: CUSTOMER takes part in the information
  sharing of `DELIVERY_SLA` (`CUSTOMER[*] -> DISPATCHER : delivery_request`,
  `DISPATCHER -> CUSTOMER[*] : status_update`, as in Section 7.2 of the
  specification) and has a view. `examples/smart_city_traffic.cadl`: the
  CONTROLLER answers an emergency request with `priority_ack` in
  `EMERGENCY_OVERRIDE`, and EMERGENCY_SERVICE declares an interface. CADL
  Explorer 0.5.6 reported a CUSTOMER that exchanged no message and an open
  control loop from EMERGENCY_SERVICE. `cadl verify` results are unchanged
  (8/8, 19/19).

## [0.3.16] — 2026-10-18

### Fixed
- `cadl ai --verify` (`generate_cadl(..., verify=True)`) reported a
  failure for every generated definition: it read a `consistent` attribute
  that the list returned by `verify()` does not have, and imported
  `check_deadlocks`, which does not exist (the function is
  `detect_deadlocks`). It now runs the same SMT and deadlock checks as
  `cadl verify` and reports only the checks whose status is `failed`, so a
  definition that `cadl verify` passes is accepted without a retry.

## [0.3.15] — 2026-10-17

### Changed
- README (English and Japanese) and `scripts/sos_dsl_handson_e2e.sh`: the
  hands-on links point to the textbook (`/docs/textbook/`) of the
  specification site, which replaced the Course A pages
  `handson/main-textbook` and `handson/exercises`.

## [0.3.14] — 2026-10-15

### Fixed
- Unity C# runtime: `ContractRuntime.PostEvent` takes an optional
  `instanceId` and, when it is given, delivers the event to that instance
  only, as `post_event(instance_id=...)` does in the Python reference
  runtime. Before, every event went to every open instance, so a host that
  drives one contract instance per robot (such as the simulator's
  `PilotContractBridge`) advanced all robots' contracts on one robot's
  event. Calls without `instanceId` behave as before.

## [0.3.13] — 2026-10-15

### Fixed
- An unquoted `true` or `false` used as a lifecycle state, a `from` / `to`
  / `initial` state, an `on_violation` or `on_match` transition, a
  violation label, or a regime name is reported as a reserved word, as it
  already was for ids. Up to 0.3.12 YAML read it as a boolean, the name
  became `True` / `False`, and `cadl check` accepted it. The IR keeps it as
  written (`true`).
- README (English and Japanese): the type-check list names the lenient
  forms of `deadline:` and `sampling:` that 0.3.12 accepts.
- CHANGELOG: the 0.3.9 section lists the `examples/test_raspimouse.sh` fix
  that was part of that release.

## [0.3.12] — 2026-10-15

A `deadline:` or `sampling:` value that cannot be read is an error. Such a
file passed `cadl check` before, and the value was lost or misread.

### Fixed
- `cadl check` reports a `deadline:` that is not a duration as an error
  (Appendix E, E.3: an integer followed by `ms`, `s`, `min`, or `h`), for
  example `deadline: soon`, `5sec`, `1.5s`, `""`, a negative value, or
  `yes`. Up to 0.3.11 the file passed; the IR had `"deadline_ms": null`, or,
  for `yes` / `no` and negative numbers, a meaningless value such as 1000 or
  -5000. A non-negative plain number is still read as seconds (a lenient
  form).
- `cadl check` reports a `sampling:` that is neither `event` nor
  `periodic(<duration>)` as an error, for example `periodic(soon)` or
  `every 500ms`. The mapping forms `{kind: periodic, period_ms: <int>}`
  with a non-negative integer and `{kind: event}` are still accepted. Up
  to 0.3.11 such a monitor was read as event-driven without a diagnostic.
- README (English and Japanese): the type-check list includes both checks.

## [0.3.11] — 2026-10-13

Rule M-1 of the SoS-DSL extension is an error. A file with an `observe:`
entry whose object is not a declared actor passed 0.3.10 with a warning
and is rejected now.

### Changed
- Rule M-1 of Appendix E is an error of `cadl check`; 0.3.10 reported it
  as a warning. An `observe:` entry whose object is not a declared actor
  (`OBSTACLES.positions` without an actor `OBSTACLES`), or that is neither
  an attribute of an actor nor a name, now makes the file invalid.
- `examples/sos_dsl_robot_delivery.cadl`: the monitor `collision_watch`
  observes `DISPATCHER.obstacle_positions` (was `OBSTACLES.positions`,
  which named no declared actor), so the file conforms to M-1 and
  `cadl check` prints no warning for it. The lifecycle, the monitor ids and
  the generated C# file names are unchanged.
- README (English and Japanese): the type-check list says that M-1 is
  checked as an error.

## [0.3.10] — 2026-10-12

`cadl check` applies the static semantics of the SoS-DSL extension. Some
files that 0.3.9 accepted are errors now: a file whose lifecycle or
monitors break rules L-1, L-2, L-3, L-5 or M-3 of Appendix E, and a file
with an `autonomy:` other than `low`, `medium`, `high`.

### Added
- `cadl check` applies Appendix E, E.4. Errors: an initial or terminal
  state that is not listed in `states` (L-1); a `from` or `to` that is not
  listed (L-2); no terminal state, or none reachable from the initial state
  (L-3); an `on_violation.transition` or `on_match.transition` that is not
  a state (L-5, M-3); a lifecycle state, transition id, monitor id or
  violation label that is not an ASCII identifier or is a reserved word; a
  duplicate transition or monitor id; a monitor without a `rule`; an
  undeclared actor in a message event of `on:`. Warnings: a `deadline:`
  without `on_violation:` (L-4); an `observe:` entry whose object is not a
  declared actor (M-1); an `on:`, `when:` or `rule:` that is not a rule of
  E.3. M-1 is a warning and not an error because the shipped example
  `sos_dsl_robot_delivery.cadl` observes `OBSTACLES.positions`; `cadl check`
  prints that warning for it.
- `cadl.parser.parse_rule` parses a rule, including `x IN [a, b]`.

### Changed
- An `autonomy:` other than `low`, `medium`, `high` is an error of
  `cadl check`; it was read as `medium` without a diagnostic (Appendix A,
  A.3).

### Fixed
- The `else:` branch of a conditional step was discarded, and the condition
  of `barrier: <predicate>` was replaced by the name `barrier`. Both are
  kept now, so actors in an `else:` branch are checked and the simulator IR
  contains the branch and the condition (Appendix A, A.5). No example uses
  either form, so the IR of every example is unchanged.
- README (English and Japanese): the type-check list covers the SoS-DSL
  rules and the `autonomy:` check.

## [0.3.9] — 2026-10-11

`cadl check` now reports the cases in which the reference implementation
accepted a file silently although the specification (Appendix A, C, D)
asks for a diagnostic. Some files that 0.3.8 accepted are errors now: a
file with a non-ASCII or reserved id, and a file with a `sharing:` entry
that is unquoted or whose item is not an ASCII identifier.

### Added
- `cadl check` reports an actor, contract, protocol, or metric id, or a
  regime name, that is not an ASCII identifier, or that is a reserved
  word (`AND`, `OR`, `NOT`, `IN`, `in`, `exists`, `true`, `false`), as an
  error (Appendix A, A.1 and A.11). An id written as an unquoted `true`
  or `false` is reported as the reserved word it is.
- A `sharing:` entry that is not a string of the form
  `"SOURCE -> TARGET : item"` with an ASCII identifier as item is an
  error (Appendix A, A.4). An unquoted entry used to be dropped without
  a diagnostic. An entry whose item began with a digit or contained
  non-ASCII letters used to be kept; it is an error now and no longer
  reaches the IR. An item followed by arguments
  (`"A -> B : report(period: 5s)"`) is still accepted, now with a
  warning that only the name is kept.
- A `codegen:` entry whose target `cadl codegen` cannot emit gets a
  warning from `cadl check`, `cadl verify`, and `cadl codegen`
  (Appendix D, D.4). `cadl codegen` now prints every type-check warning,
  not only this one. An entry without `target:` means `python`, as A.9
  says.
- Informational diagnostics (`[INFO]`, never a failure) for an
  `extensions:` name the implementation does not know, for a version of
  `sos-dsl` other than `0.1`, and for a `motivation:` block
  (Appendix C). `cadl verify --format json` lists them under
  `type_check.infos`.
- The `motivation:` block is read and passed on as written by
  `cadl sim-ir` as `motivation_block` (Appendix C, C.6). The key is
  present only when the source has the block, so the IR of other files
  is unchanged. No content of the block can make a file invalid; in
  JSON output, values that JSON cannot express (dates, infinities) are
  written as text.

### Fixed
- `examples/test_raspimouse.sh` exited with status 1 after generating the
  configs, because its final `ls` quoted the glob.

### Changed
- `examples/raspimouse_c_sos.cadl`: the four `sharing:` entries of the
  delivery-assignment contract no longer carry arguments. The generated
  IR and simulator configs are byte-identical.
- README (English and Japanese): the type-check list covers the new
  checks and diagnostics.
- README (English and Japanese): links to the specification, hands-on
  course, changelog, contributing guide and issue tracker at the top; the
  `verification` block and the SoS-DSL `lifecycle:` / `monitors:` blocks in
  the language overview; the Unity C# command under Usage; the pipeline
  diagram says "Checked AST"; the type-check list matches what `cadl check`
  does; the examples table counts regimes consistently (`supply_chain` has 4,
  `c_sos_taxi_fleet` 3) and lists `sos_dsl_robot_delivery.cadl`;
  `demo_sim_ir.py` under demo scripts; `cadl-spec` and `cadl-explorer` under
  related projects.
- CONTRIBUTING: CI runs Python 3.9 through 3.14; a release also updates
  `version:` in `CITATION.cff`.
- README (English and Japanese): the workshop takes about 95 minutes (was
  "90-minute"); the simulator entries point to the public export,
  `cadl-raspimouse-simulator`; the SoSE 2026 paper is in the English
  reference list as well.
- `.gitignore`: the files written by the README commands (`regime.dot`,
  `sim_config.json`, `sim_config.yaml`).

## [0.3.8] — 2026-10-09

Python 3.14 is now a tested and declared version.

### Fixed
- The event-loop tests used `asyncio.get_event_loop()`, which raises on
  Python 3.14 when no loop is running. They use `asyncio.run()` now, and
  Python 3.14 is part of the CI matrix. The library itself needed no change.

## [0.3.7] — 2026-10-09

Second round of fixes from the cross-check against the specification.

### Fixed
- A name that begins with a keyword was split into the keyword and the
  rest: `NOT_READY` was read as `NOT _READY`, `NOTIFIED == true` as
  `NOT IFIED == true`, and a declared actor `NOTIFIER` was reported as
  undefined actor `IFIER`. `a ORb` and `for all x inventory: ...` were split
  the same way. Keywords now end at a word boundary.
- `cadl check` reported `parties: ["ROBOT[1]", "ROBOT[2]"]` as a duplicate
  party; two references are the same party only when name and index agree.
- `cadl check` did not look for undeclared actors inside an index
  (`ROBOT[GHOST.n].x`).
- A `severity:` other than `Minor`, `Major`, `Critical` passed `cadl check`
  and reached the generated C# as an enum member that does not exist; it is
  now an error.
- `sampling:` written as a mapping with an unknown `kind` or a non-integer
  `period_ms` was carried into the IR as written; it is now read as `event`,
  like an unrecognised string form.
- `verification:` entries: an `expr:` that is not a predicate of Appendix A
  was reported as satisfiable (now `unknown`, not checked); an unquoted
  `expr: false` was read as the name `False`; `target:` is checked for every
  method, and white space around the arrow of `FROM -> TO` is ignored.

### Changed
- README: the pipeline diagram shows Unity C# among the code targets, and
  the code generation section says that `cadl codegen` does not run the
  verifier.

## [0.3.6] — 2026-10-09

### Changed
- Generated Solidity files carry `SPDX-License-Identifier: Apache-2.0`
  (was `MIT`), matching the license of the toolchain. Edit the line if you
  release generated contracts under another license.
- The default model of `cadl ai` is `claude-sonnet-5-5` (was
  `claude-sonnet-4-20250514`). Set `CADL_LLM_MODEL` or pass `--model` to
  use another one.
- The CI and publish workflows declare `permissions: contents: read`; the
  publish job keeps `id-token: write` for Trusted Publishing.

## [0.3.5] — 2026-10-09

### Fixed
- `cadl codegen --target unity-csharp` emitted the `case` lines of
  `IsTerminal` in an order that changed from run to run (the terminal states
  were iterated as a set), so regenerating from an unchanged file produced a
  different `*Contract.cs` each time. They now follow the order in which
  `terminal:` declares them. A test regenerates every code target under
  several hash seeds and requires identical output.

## [0.3.4] — 2026-10-09

Aligns the implementation with the specification (Appendix A / E) where a
cross-check found them apart.

### Fixed
- `cadl check` reported a predicate that is a single name (`"system_ready"`)
  as an undefined actor, and did not look for undeclared actors inside
  comparisons or `AND` / `OR` / `NOT`. A name is now treated as an actor,
  and must be declared, when it is indexed (`ROBOT[i]`) or is the object of a
  member access (`GHOST.x > 5`); a bare name is taken as a state variable.
  Variables bound by a quantifier or a comprehension are exempt.
- A quantifier could only start an expression. It may now follow `AND`,
  `OR` and `NOT` as in spec A.10 (`a AND for all x in X: p(x)`), and scopes
  over everything to its right.
- `in`, `exists` and `IN` were accepted as identifiers although the spec
  reserves them.
- A `verification:` entry with `method: smt` passed whatever it contained.
  It now fails when `target:` does not name a declared contract, protocol,
  regime or transition, or when `expr:` is unsatisfiable. `expr` is still
  not proved against the model; the message says so.
- `scripts/sos_dsl_handson_e2e.sh` pointed to a runbook file that does not
  exist; it now points to the hands-on textbook.
- The NL-to-CADL prompt listed autonomy levels `none` and `full`, which the
  language does not have.

### Changed
- IEC 62853 summary: `lambda` is labelled "incentive strength" and described
  on the scale the specification defines (0 = directive-based, 1 = market
  mechanism) instead of as "incentive alignment" / "misaligned incentives".
- README: lists `unity-csharp` as the fourth code generation target and
  completes the project structure listing.

## [0.3.3] — 2026-10-09

### Security
- `cadl codegen` for the `python`, `solidity` and `opa` targets wrote ids and
  text from the `.cadl` file into generated source verbatim. A crafted
  string could add code to the generated module, and a contract id such as
  `../../x` wrote a file outside the output directory. Code generation now
  checks the definition first and refuses ids that are not identifiers and
  text containing quotes, backslashes, backticks, `*/` or control
  characters (`cadl.codegen.safety`). The Unity C# target already sanitised
  names and escaped literals.

### Changed
- License changed from MIT to Apache License 2.0 (`LICENSE`, `NOTICE`).
  Releases up to and including 0.3.2 remain available under the MIT License.
- `cadl iec62853` is described as an IEC 62853-oriented dependability
  summary instead of a "compliance report". The indicator names are defined
  by CADL, not by the standard, and the report does not assess conformance;
  the text output and a new `disclaimer` key in the JSON say so. JSON keys
  are otherwise unchanged.
- The hands-on script and documentation point to the public
  `cadl-raspimouse-simulator` repository and to Unity 6 (6000.2);
  `sos_dsl_handson_e2e.sh` now defaults to `../cadl-raspimouse-simulator/unity`.
- `beta` is documented consistently as decision **centralization**
  (0 = fully distributed, 1 = centralized), which is how the IEC 62853
  report, the simulator IR and most examples already used it. The README
  definition and the NL-to-CADL prompt said the opposite. Values written
  under the old wording were mirrored (`1 - beta`):
  `examples/robot_delivery.cadl` 0.2 → 0.8,
  `examples/household_chores.cadl` 0.3 → 0.7, and the README / prompt
  samples.
- README: dropped the links to the instructor course-design page (removed
  from the spec site) and marked `raspimouse-swarm-simulator` as not
  publicly available.

### Fixed
- Parser ignored `method:`, `expr:` and `bound:` in `verification:` entries,
  so `method: model_check` (or `simulation` / `proof`) written in a `.cadl`
  file was treated as the default `smt` and reported as passed. The fields
  are now read, and non-SMT methods surface as `not_supported` from
  `cadl verify`, as the 0.3.0 entry describes.
- `scripts/sos_dsl_handson_e2e.sh` ran a command quoted in one of its
  comments on every invocation, leaving a stray `dd/` copy of `src/` in the
  repository on case-insensitive file systems.

### Added
- `.github/workflows/publish.yml`: publishing a GitHub Release builds the
  distributions and uploads them to PyPI through Trusted Publishing.

## [0.3.2] — 2026-10-09

### Fixed
- Expression parser dropped the arithmetic operators, so every expression
  containing `+`, `-`, `*` or `/` failed to parse and fell back to an opaque
  string (reported as "not a valid expression").
- Expressions containing `AND` were returned as a raw parse tree instead of
  an AST node, and `OR` bound tighter than `AND`. Precedence is now, loosest
  to tightest: `OR`, `AND`, `NOT`, comparison, `+ -`, `* /`. A quantifier
  scopes over everything to its right.
- Verifier encoded every name as a boolean coerced to 0/1, so thresholds
  such as `battery > 20` were unsatisfiable. Names used in comparisons or
  arithmetic are now real-valued; the same call text maps to the same symbol.
- Deadlock detector reported any request followed by its reply as a circular
  dependency. Sequential steps are ordered and no longer count as waits;
  only mutual sends between `parallel` branches do.
- `cadl verify` crashed when printing a `not_supported` result.
- Generated labels and simulator IR text showed AST class names
  (`FunctionCall`, `ActorRef <= DurationLiteral`) or lost indices for parsed
  expressions; they now show the CADL source text.
- Python codegen resolved a quantifier's bound variable through the actor
  table (`ctx.actors['r']`).
- Generated Python and Solidity read every bare name from the actor table
  (`ctx.actors['delivery_time']`, `actors_delivery_time`). Names that are not
  declared actors are now state variables: `ctx.state['delivery_time']` in
  Python, `stateUint["..."]` / `stateBool["..."]` in Solidity.
- Generated class and file names lower-cased the inside of camel-case names
  (`Robotdeliverysystem.sol`); they now keep it (`RobotDeliverySystem.sol`).

### Changed
- "Assumes do not entail guarantees" is reported as `[INFO]`, not `[FAIL]`:
  guarantees are obligations, not consequences of the assumptions. `verify`
  instead fails a contract whose assumptions contradict each other
  (`Contract '<id>' assumptions`), per the spec's `all_assumes_satisfiable`.
- `[INFO]` and `[SKIP]` results are excluded from the pass/fail totals.
- Examples: transitions leaving the same regime now have mutually exclusive
  conditions (`a_sos_robot_delivery`, `c_sos_taxi_fleet`,
  `smart_city_traffic`, `supply_chain`, `raspimouse_mcp_sos`); the MCP
  example uses `for all` / `exists` in place of `all(...)` / `any(...)`.
  Every example now passes `cadl verify`, and CI enforces it.

- **Generated names changed**: the runtime class and the Solidity
  orchestrator of an SoS with a camel-case name are now spelled as in the
  source, e.g. `RobotDeliverySystemRuntime` (was `RobotdeliverysystemRuntime`).

### Added
- Member access on an indexed actor: `ROBOT[i].battery`, `ROBOT[*].status`.
- Aggregate comprehensions as call arguments:
  `sum(ROBOT[i].goal_count for i in 1..5)`, `sum(r.load for r in ROBOT[*])`.
- `cadl.unparse.expr_to_source()` renders an expression back to CADL text.
- `CONTRIBUTING.md`, `SECURITY.md` and `CITATION.cff`.

## [0.3.1] — 2026-10-09

### Changed
- PyPI distribution renamed from `cadl` to `cadl-lang` (the name `cadl` is
  taken on PyPI by an unrelated project). The import package (`import cadl`)
  and the `cadl` CLI command are unchanged.
- The package version is now read from `cadl.__version__` only
  (`pyproject.toml` no longer carries a second copy).
- CI runs on Python 3.13 as well, and smoke-tests parse / check /
  sim-validate / codegen / sim-gen on every example instead of three.
- README: installation from PyPI, feature status table in place of the
  development-phase roadmap, updated hands-on links.

### Fixed
- Unity C# codegen emitted `break` instead of `return` in transition guards.
- `scripts/sos_dsl_handson_e2e.sh` recovers from a nested
  `Generated/` / `Runtime/` directory left by an earlier run.
- Solidity generator tests expected file names in a different letter case
  from what the generator writes, so they failed on case-sensitive file
  systems (CI on Linux).

## [0.3.0] — 2026-04-28

### Added
- SoS-DSL extension (spec Appendix E): `lifecycle:` and `monitors:` blocks
  in the parser, carried through to the simulator IR.
- Unity C# code generation target for the SoS-DSL extension, with an
  end-to-end script (`scripts/sos_dsl_handson_e2e.sh`) and structural lint
  tests for the generated C#.
- FCFS task arbitration and motivation config in the simulator IR and the
  Unity config generator.
- `VerificationSpec.method` / `expr` / `bound` fields and
  `cadl.verifier.dispatch_spec()` implementing explicit `not_supported`
  results for `model_check` / `simulation` / `proof` methods per
  Appendix A §A.8 (previously these were silently skipped).
- Optional `MotivationBlock` / `AgentMotivationBlock` /
  `GovernanceMotivationBlock` AST nodes and `SoSDefinition.motivation`
  field for CADL Appendix C (v0.1-ext) motivation extension parity with
  `cadl-explorer`.
- `LICENSE` (MIT) and full packaging metadata in `pyproject.toml`.
- GitHub Actions workflow running `pytest` on Python 3.9–3.12 and building
  the sdist + wheel on every push.
- `CHANGELOG.md` (this file).
- Hands-on section in the README pointing to the `cadl-spec` materials.

### Changed
- Fixed attribution in `README.md` / `README_ja.md` from "Matsubara
  Laboratory" / "松原研究室" to "ERTL".
- `CodegenSpec.target` docstring updated to enumerate the canonical
  target catalog (`unity` / `ros2` / `python` / `solidity` / `opa` /
  user identifier) to match Appendix D of the spec.

### Fixed
- Unity C# `PredicateEvaluator` emitted an invalid regex literal.

## [0.2.2] — 2026-03-23

### Fixed
- Cyrillic characters in identifiers in `examples/raspimouse_c_sos.cadl`.

## [0.2.1] — 2026-03-22

### Changed
- README updated with the Raspimouse examples.

## [0.2.0] — 2026-03-22

### Added
- Regime transition analysis and regime map (`regime_map.py`,
  `cadl regime-map`).
- Multi-target code generation: Solidity smart contracts
  (`codegen/solidity/`) and OPA / Rego policies (`codegen/opa/`) next to
  the Python runtime (`cadl codegen --target`).
- IEC 62853 report (`iec62853.py`, `cadl iec62853`).
- Three-layer simulator IR (Institution / Protocol / Algorithm) and config
  generators for Python, Unity and Go simulators (`cadl/sim/`).
- Examples: supply chain, smart city traffic, A-SoS robot delivery, C-SoS
  taxi fleet, three Raspimouse swarm modes, and demo scripts including an
  A-SoS vs C-SoS governance comparison.

### Fixed
- String values in `environment:` were parsed as expressions.

## [0.1.0] — 2026-03-17

Initial draft of the CADL compiler (not tagged).

### Added
- Lark grammar (`src/cadl/grammar.lark`) and parser producing an AST.
- Type checker and deadlock analyzer (`type_checker.py`, `deadlock.py`).
- Verifier integrating Z3 for properties declared in `verification:`
  blocks.
- Python runtime code generation (actor / contract / metric / protocol /
  transition scaffolds).
- AI-assisted natural-language → CADL translation (`ai/`), gated behind
  the optional `ai` extra (requires `anthropic`).
- CLI entry point `cadl`.
- Example CADL files under `examples/` and a pytest suite.

[Unreleased]: https://github.com/ertlnagoya/cadl/compare/v0.3.18...HEAD
[0.3.18]: https://github.com/ertlnagoya/cadl/compare/v0.3.17...v0.3.18
[0.3.17]: https://github.com/ertlnagoya/cadl/compare/v0.3.16...v0.3.17
[0.3.16]: https://github.com/ertlnagoya/cadl/compare/v0.3.15...v0.3.16
[0.3.15]: https://github.com/ertlnagoya/cadl/compare/v0.3.14...v0.3.15
[0.3.14]: https://github.com/ertlnagoya/cadl/compare/v0.3.13...v0.3.14
[0.3.13]: https://github.com/ertlnagoya/cadl/compare/v0.3.12...v0.3.13
[0.3.12]: https://github.com/ertlnagoya/cadl/compare/v0.3.11...v0.3.12
[0.3.11]: https://github.com/ertlnagoya/cadl/compare/v0.3.10...v0.3.11
[0.3.10]: https://github.com/ertlnagoya/cadl/compare/v0.3.9...v0.3.10
[0.3.9]: https://github.com/ertlnagoya/cadl/compare/v0.3.8...v0.3.9
[0.3.8]: https://github.com/ertlnagoya/cadl/compare/v0.3.7...v0.3.8
[0.3.7]: https://github.com/ertlnagoya/cadl/compare/v0.3.6...v0.3.7
[0.3.6]: https://github.com/ertlnagoya/cadl/compare/v0.3.5...v0.3.6
[0.3.5]: https://github.com/ertlnagoya/cadl/compare/v0.3.4...v0.3.5
[0.3.4]: https://github.com/ertlnagoya/cadl/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/ertlnagoya/cadl/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/ertlnagoya/cadl/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/ertlnagoya/cadl/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/ertlnagoya/cadl/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/ertlnagoya/cadl/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/ertlnagoya/cadl/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/ertlnagoya/cadl/releases/tag/v0.2.0
