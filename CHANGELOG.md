# Changelog

All notable changes to the CADL language and toolchain are documented in
this file. The format follows [Keep a Changelog](https://keepachangelog.com/)
and the project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- `cadl norms-check` (F.12) covers values of every type. Each quantity
  has classes for no value, true, false, numbers (around the numeric
  literals, 0, and 1 next to a boolean literal), texts (each literal and
  one other) and lists and objects, and every predicate is evaluated on
  the representatives by the evaluator of `cadl norms-eval`. A comparison
  of values of different types now has the value it has in a trace (a
  boolean is ordered as 0 or 1 but is not equal to 1; a text is not
  ordered against a number), and the note that the answers assume the
  type of the literals is gone. Up to 0.3.25 such traces were not
  covered: a prohibition violated only by `true` was reported as never
  violated, and a conflict that a text value avoids was reported.

### Fixed

- `cadl game-check` and `cadl model-check` no longer stop with
  `ValueError: Exceeds the limit (4300 digits) for integer string
  conversion` when an exact result (I.13, H.8) has a numerator or
  denominator of more than 4300 digits, as a model with a few hundred
  steps can have. Exact numbers are now written by
  `cadl.stochastic.int_text` and `fraction_text`, which split such an
  integer by powers of ten and leave the limit of the process unchanged;
  the output is the same as with the limit lifted.

## [0.3.25] — 2026-11-12

Design-time checking of norms, norms monitors in two more generated
runtimes, and a gateway for A2A. `cadl norms-check` translates the norms
of one contract instance into a model for PRISM and asks whether each
norm can be activated, violated and fulfilled, whether every activation
can be kept, which obligations and prohibitions are in conflict, and
whether powers and reparations are ever used, each answer with a trace
that the norms engine confirms (Appendix F, F.12). `cadl codegen -t opa`
and `-t solidity` now write a monitor of the norms that follows F.5
observation by observation (Appendix D, D.5). `cadl a2a-gateway` decides
the messages of an A2A 1.0.0 agent with the same decision function,
approvals and audit log as the MCP gateway (Appendix G, G.13). Each part
was reviewed independently, the gateway in five rounds, before the
release.

Known limits: `norms-check` does not translate quantified predicates,
arithmetic, several instances or amendments, and assumes that values
have the type of the literals they are compared with; the monitors
compute arithmetic and times differently from `cadl norms-eval` in the
ways D.5 lists, and the Solidity monitor is tested on an EVM outside CI;
the A2A gateway was tested against a fake agent and client, supports
the JSON-RPC binding without streaming, and cannot keep many hosts that
connect together from filling its readers.

### Added

- Norms monitors in the generated Rego and Solidity (Appendix F, F.5;
  Appendix D). For a file that declares `norms: 0.1`, `cadl codegen -t
  opa` writes `norms_monitor_<contract>.rego` per contract: a step
  function that takes the previous state of the monitor (plain JSON,
  whose keys Appendix D lists) and one observation, and returns the next
  state, the changes of the norm states and verdicts, the violations,
  institutional events and unauthorised acts, and the verdicts.
  `cadl codegen -t solidity` writes `<Contract>NormsMonitor.sol`: a
  contract that keeps that state, with `begin`, `observe` (the time is an
  argument, fixed-point with 6 decimals) and `finish`, `verdicts()` and
  events, and two libraries it links to stay under the size limit of
  EIP-170. Both follow the order of F.5 of `cadl norms-eval`, with
  message and institutional events, three-valued predicates, triggers,
  `until:`, deadlines, reparations, powers, counts-as rules and the
  temporal operators. A contract with a quantifier, an aggregate, a
  wildcard or range reference or an index expression in a norm, a
  counts-as rule or an `assume:` item gets no monitor, with a warning
  that says why; such a `guarantee:` item is left out of the monitor
  with a warning. Amendments are refused at the observation that has
  one. The Solidity monitor also refuses `*`, `/`, numbers with more
  than 6 decimals, nested temporal operators and a monitor whose
  creation code would exceed the limit of EIP-3860 (in a `guarantee:`
  item only the item is left out); only its reporter (an address given
  to the constructor, by default the deployer) may record observations,
  and an observation costs gas independent of the length of the trace
  (windows are scanned once, values found by binary search and pruned),
  except about 4 x 10^4 gas per observation inside an open window of a
  temporal content, which `cadl codegen` names in a warning; a bare name
  read inside a temporal operator that becomes a quantity after the
  first observation is refused (give it in `begin`).
  A binding must be an integer written as one or a string. The tests compare both with the engine of `cadl
  norms-eval` after every observation (the example traces, the traces of
  `tests/test_norms_eval.py` and random traces; OPA with `opa eval`,
  Solidity on an EVM from PyPI in a separate Python named by
  `CADL_EVM_PYTHON`) and check that hostile strings stay data. Up to
  0.3.24 these targets left out the norms and the temporal items with a
  warning; the files they generated before are unchanged.
- `cadl norms-check FILE [--contract ID] [--bindings i=n] [--members SET=n]
  [--step DURATION] [--horizon N] [--export DIR] [--format text|json]`:
  design-time checking of the norms of one contract instance by a model
  checker (Appendix F, F.12; new module `cadl.norms_check`). The norms
  are translated into an MDP in the PRISM language whose environment
  chooses, at each observation (one every step, the greatest common
  divisor of the durations by default), the message events, one class of
  values per quantity (the classes that the literals of the predicates
  cut the domain into, and no value) and the end of the instance; one
  transition is the six steps of F.5. PRISM (`prism` on PATH, explicit
  engine) answers per norm whether it can be activated, violated and
  fulfilled, whether it is violated in every instance that runs to the
  horizon, whether every activation can be kept, which pairs of
  obligations and prohibitions are in conflict, whether each power can
  be exercised and each reparation activated. Every "possible" answer
  comes with a witness trace that the command evaluates with the engine
  of `cadl norms-eval` (two for a conflict). `--export` writes the model,
  the two property files and the witnesses. Predicates with
  quantifiers, aggregates, arithmetic or two quantities, `request`,
  `now`, `state` and amendments are not translated (the norm, and the
  norms that depend on it, are reported with the reason). Without PRISM
  the command reports that nothing was checked (exit status 0). Exit
  status 1 on a finding, 2 on a usage error, refusal or timeout, 3 when a
  witness is not confirmed. `--timeout SECONDS` (default 600) bounds all
  runs of PRISM together; `--require-checker` makes a missing PRISM an
  error (exit status 2). An activation that cannot be kept is asked in
  the state before the activating observation, so an obligation with a
  deadline of 0 s whose content may come at its activation is not
  reported; its witness is confirmed for every choice at that
  observation. The classes of values assume values of the type of the
  literals, which the report notes. `examples/norms_check_conflict.cadl` shows a
  conflict, a reparation that cannot be kept and a power that is never
  exercised. The translation is tested differentially against
  `cadl norms-eval` on random paths and on every path of at most two observations of one contract, with an interpreter
  of the generated PRISM text that is itself compared with the PRISM
  simulator.
- `cadl a2a-gateway FILE --agent-actor ACTOR --upstream URL --client-keys
  KEYS.json --audit OUT.jsonl [...]` (ships in 0.3.25): the A2A mapping of
  Appendix G, G.11, as an HTTP server for the JSON-RPC binding of A2A
  1.0.0 in front of one A2A agent. Every request is authenticated with
  the key of its actor (`Authorization: CADL-HMAC`, an HMAC-SHA256 bound
  to the run, a nonce used once, the path and the SHA-256 of the body);
  an unauthenticated request gets HTTP 401 and is recorded
  (`unauthenticated_request`). A `SendMessage` names its CADL message and
  arguments with the extension
  `https://www.ertl.jp/cadl-spec/a2a/agents-message/v0.1` and is decided
  by the online runtime: allowed requests are passed on re-serialised,
  denied ones get a task in `TASK_STATE_REJECTED`, escalated ones are held
  (blocking, or a `TASK_STATE_SUBMITTED` task of the gateway with
  `returnImmediately`) until an approval through the inbox
  (`cadl mcp-approve`), a rejection or the timeout. `GetTask` and
  `CancelTask` pass for the client's own tasks only (`task_refused`);
  streaming, push notifications, `ListTasks`, `GetExtendedAgentCard`,
  `tenant` and every other method are refused. A held request is
  withdrawn when its client cancels the task or closes the connection.
  Optional HTTPS (`--tls-cert`, `--tls-key`); an address other than
  loopback needs it. The trace replays through `cadl agents-run` to the
  same audit log, byte for byte. New host events `unauthenticated_request`,
  `task_refused` and `invalid_response`, which `agents-run` reads.
- `cadl.gateway_core`: the enforcement point, trace, audit log, holds,
  inbox and inbox tools shared by the two gateways, factored out of
  `cadl.mcp_gateway`, which re-exports its former names.

### Security

- Review of `cadl a2a-gateway` before its release: a context id that the
  gateway had not seen given to the client was claimed by whoever named it
  first, also across runs, so a client could continue another client's
  context; it is now refused. `tenant` could select another tenant's task
  with the same id; it is refused. An approval rule whose receiver is a
  set (`OPERATOR -> WORKER[*] : approve`) was not recognised, so an
  approval could come as an A2A message without the binding to the held
  arguments; approval names are now taken from every such rule, also for
  the inbox of `cadl mcp-message`. Over TLS, a client that closed the
  connection of a held request with close_notify was not seen as gone.
  Unauthenticated requests are recorded at most 20 times a second.
- Second review of `cadl a2a-gateway`. An approval rule whose receiver is
  a set or a range now names the gateway's own actor when it is a member
  (`mcp-approve` works, bound to the arguments), for the A2A and the MCP
  gateway; a gateway whose actor's contract escalates with no approval
  rule it can name refuses to start. A client that sends more bytes and
  then closes, or half-closes, withdraws its held request. A cancellation
  or disconnect after a deadline that the ticker had not yet observed is
  answered from the closing record (`expired`). `CancelTask` is recorded
  (`task_cancel`). A released `returnImmediately` request that cannot be
  passed on fails its task (`forward_failed`). `--agent-card` has to
  declare the extension; `--upstream-token` is refused with a plain-HTTP
  agent off loopback; at most `--max-handlers` (64) requests at a time,
  more get 503 (`overloaded`); at most 1 000 000 nonces per run.
- Third review of `cadl a2a-gateway`. A request has to be read within
  `--read-timeout` (10 s) in all (`read_timeout`); at most `--max-readers`
  (64) connections are read before authentication; a `--max-handlers`
  slot is taken only after authentication; one actor may hold at most
  16 blocking requests (later `--max-handlers-per-actor`). Slow unauthenticated
  connections could otherwise lock out every client. Approval rules with
  a set, range or index-variable receiver resolve to the gateway's
  actor, then (MCP) its client, then the first declared member, so files
  that 0.3.24 accepted start again; only an escalation without any
  approval rule is refused. The nonce cap is per actor.
- Fourth review of `cadl a2a-gateway`. Peers of one address that
  reconnected at once kept every reader busy: at most
  `--max-readers-per-address` (4) connections of one address (an IPv4
  address or the /64 prefix of an IPv6 address) are read at
  once (more are closed at once), and a connection waits up to one second
  for a reader before 503. One actor uses at most
  `--max-handlers-per-actor` (16) handlers, which replaces
  `--max-held-per-actor` and also covers requests to a slow agent, and
  has at most `--max-holds-per-actor` (64) holds, `returnImmediately`
  included (503 before the decision, `overloaded`).

## [0.3.24] — 2026-11-11

Extensions of the norms, strategic model and agents tooling. Wildcard
and range references such as `ROBOT[*]` are now evaluated as the
quantifier or comprehension they stand for (F.5), aggregates that only
grow or only shrink are decided over open domains, and the grounding
indices are computed at the level of a system of systems. The strategic
model checker certifies the end points of any strategic model and
offers the concept `every_reachable_state` (Appendix I). The agent
runtime keeps a tamper-evident audit log (`cadl audit-verify`) and an
MCP gateway (`cadl mcp-gateway`) that decides every tool call of an
agent against the contract, holds calls that need approval, and accepts
approvals signed with per-approver keys (Appendix G). Each part was
reviewed independently before the release.

Known limits: the gateway was tested against a test MCP server and
client written for the purpose, not against a released MCP SDK; the
approver's tool trusts the description of a held call that the gateway
publishes; the strategic export was not run through PRISM-games.

### Added
- Tamper-evident audit log (Appendix G, G.8, "Integrity of the log"):
  `cadl agents-run --audit` writes a header record (format
  `cadl-audit/1`, SHA-256 of the CADL file, epoch, hash algorithm), the
  records of G.8, and an end record with their count; every line carries
  `seq`, `prev` (the digest of the previous line) and `digest` (SHA-256
  of its canonical JSON). `--audit-key FILE` keys the chain with
  HMAC-SHA256. Standard library only. Without a key the chain detects
  any changed byte, deleted, inserted or reordered line, changed header
  and cut tail, but not a rewrite of the whole log; with a key only a
  holder of the key can rewrite it.
- `cadl audit-verify AUDIT.jsonl [--key FILE] [--source FILE.cadl]
  [--open] [--format text|json]`: verifies the chain and reports the
  first line that fails, with what was expected and found. Exit status
  0 valid, 1 invalid, 2 usage error.
- Online runtime: `AgentsRuntime.open(file)`, `.observe(line)` and
  `.close(t)` drive the enforcement point one trace line at a time;
  `cadl agents-run` is a loop over the same steps.
- `cadl mcp-gateway FILE --agent ACTOR --server-actor TOOLSERVER --audit
  OUT.jsonl [...] -- SERVER_COMMAND`: an MCP stdio proxy (JSON-RPC 2.0,
  MCP specification 2026-07-28) that decides every `tools/call` with the
  runtime. Allowed calls are forwarded; denied calls get a tool result
  with `isError: true` that names the reason and the fallback; escalated
  calls are held until an approval or rejection (`cadl mcp-approve DIR
  HOLD_ID approve|reject --as ACTOR`) or the timeout. Other governed
  messages, such as grants, come in with `cadl mcp-message`. `tools/list`
  is filtered to the declared tools. Fail-closed on malformed input and
  on a failure of the decision step. The gateway writes a trace that
  `cadl agents-run` replays to the same decisions and the same audit log,
  byte for byte.

### Security
- `cadl mcp-gateway` after an independent review. A client could hide a
  complete `tools/call` inside another message with bare carriage
  returns, and a server that reads with universal newlines executed it;
  lines are now parsed strictly (no CR other than one trailing CRLF, no
  duplicate keys, no NaN, no lone surrogates) and forwarded as the
  re-serialised object with every non-ASCII character escaped, never as
  received. Methods pass by an allowlist in both directions (`tools/call`
  decided; lifecycle, discovery and `tools/list` passed; anything else
  refused and recorded as `method_refused` unless `--allow-method`);
  responses pass only for a request in flight (`unsolicited_response`).
  Names are matched in full (`"read_file\n"` stopped the gateway). The
  inbox must be a 0700 directory of the user, and only regular files with
  one link of that user are read. Approvals are authenticated: HMAC-SHA256
  per approver (`--approver-keys`, `cadl mcp-approve --key`), bound to the
  run, the hold, the held message, its time and a nonce; without keys the
  inbox is refused unless `--insecure-approvals`. A cancelled held call is
  withdrawn (hold state `withdrawn`). Lone surrogates in a trace are a
  trace error (`agents-run`, `norms-eval`) instead of a crash.
- Second review of `cadl mcp-gateway`. A number that overflows to
  infinity (`1e999`) passed the parser and made forwarding fail after the
  decision, so the log showed an allowed call that was never sent; such
  numbers, and nesting deeper than 100, are now refused, the line to send
  and the digest of the arguments are made before deciding, a failure to
  send is recorded (`forward_failed`), and the held calls that a failure
  denies are withdrawn in the runtime too. The approver sees the
  arguments of a held call (`DIR/holds/h-N.json`, `mcp-approve --show`),
  and an approval is bound to their SHA-256. The approvals mode
  (`authenticated`, `insecure`, `none`) is in the first trace line and the
  audit header; accepted approvals record the key id and the nonce, and an
  insecure one is `approval_unauthenticated`.
- Audit log: a random `run_id` in the header (and on the first line of
  the gateway's trace); numbers in the canonical form are integers or
  decimals without exponent; integers beyond 2**53 are refused;
  `audit-verify --run-id` and `--anchor`.

### Changed
- The audit file of `cadl agents-run --audit` has a header line, an end
  line, and three integrity fields at the end of every record line. The
  records themselves, their fields and their order are unchanged; read
  them with `cadl.audit_log.read_records`. `--format json` and the text
  report are unchanged. The header has a random run id unless the trace
  gives `run_id`, so two runs of the same trace differ in the header and
  the digests.
- Trace keys `run_id` (first line), `withdraw` and `host_events` for
  `cadl agents-run`; hold state `withdrawn` (counted in `summary.holds`
  only when it occurs).

- Wildcard and range references in predicates (Appendix F, F.5,
  "Wildcard and range references"). `A[*]` or `A[lo..hi]` outside the
  domain of a quantifier or comprehension is now shorthand: in the
  argument of `sum`, `count`, `min`, `max` or `avg` it ranges over the
  members (`count(ROBOT[*].status == Collision)` is
  `count(ROBOT[j].status == Collision for j in ROBOT[*])`), and anywhere
  else its smallest enclosing comparison must hold for every member
  (`ROBOT[*].battery > 10` is `for all j in ROBOT[*]: ROBOT[j].battery >
  10`); the references of one set and form in one comparison denote the
  same member, two sets in one comparison every pair. `cadl norms-eval`,
  the generated `norms_monitor.py` and `cadl agents-run` rewrite such a
  predicate when they read the contract (`desugar_wildcards` in
  `cadl.norms_eval`, in the region the monitor copies) and evaluate the
  quantified form, so open domains, the 10 000-member limit and the
  prefix property apply unchanged. An aggregate argument with two sets
  and a reference in the arguments of a message event stay not
  evaluable. Up to 0.3.23 such a predicate was not evaluated.
- Monotone aggregates in a comparison (F.5): `count`, `max` and `min`
  of a comprehension compared with an operand without a quantifier or
  comprehension are decided from the known members, over an open domain
  and with undefined elements, when every completion agrees (an
  interval evaluation: count in [true, true + undefined], unbounded
  above when open; max from the largest defined value up; min from the
  smallest down). `count(ROBOT[*].delivery == request.id) >= 2` now
  detects a double assignment while `N` has no value. `sum` and `avg`
  are not refined; an element with a value of the wrong kind still
  makes the aggregate undefined. Up to 0.3.23 such comparisons were
  undefined.
- N-15 (`cadl check`, warning): a wildcard or range reference bound
  under `NOT`, two sets in one comparison, the same set in both operands
  of `OR`, the same set on both sides of one comparison (also inside an
  aggregate), a set inside `within`/`throughout`/`until`, and an
  aggregate argument over two sets; each warning names the quantified
  form that was probably meant.
- The quantities of the SoS (Appendix J, J.4, "The quantities of the
  SoS"): alpha^_SoS, beta^_SoS and chi_SoS over all contracts of a file,
  with an item qualified by its contract and the powers of one single
  actor counted over all contracts. `cadl norms-indices` reports them
  in a last section and under the JSON key `sos` for a file with two or
  more contracts (without `--contract`); `cadl sim-ir` writes them as
  `institution.grounding`. They are not weighted and not compared with
  any declared value; tied single actors are all named, sorted, so the
  report does not depend on the order of the contracts. The output for each contract is unchanged.

- `cadl agents-run`: a norm whose `trigger:`, `until:` or `content:` has
  a wildcard or range reference outside a quantifier is now judged (it
  was not, and its in-force flag was unknown for the whole run). The
  runtime reads predicates through the norms engine, so
  `agents_runtime.py` itself is unchanged.
- The outputs of the bundled examples (`check`, `sim-ir`, `sim-gen`,
  `codegen`, `norms-eval`, `agents-run`, `norms-indices`) are identical
  to 0.3.23 except the copied engine in the generated
  `norms_monitor.py`.
- Strategic models (games 0.1, Appendix I): `concept:` on an entry of
  `equilibria:`. `concept: every_reachable_state` asks whether the
  profile is an (ε-)equilibrium from every reachable state: no player
  gains by deviating alone from any state of Reach_p, the states it
  reaches by its own deviations while the others follow the profile
  (I.7). `concept: nash` is the default. A check reports, per player,
  the largest gain from a state of Reach_p (`state_gain`), the first
  state that attains it (`state`) and the canonical best response from
  there. On a map, a cell is `equilibrium` by E2, by E1 at every state
  of the relaxed process (E1*), or from its end points; a player is a
  deviator by N or by N_s, which names a state that is in Reach_p at
  every valuation of the cell (a path of transitions whose
  probabilities are positive on the whole cell, at every corner when
  multi-affine and by exact interval arithmetic otherwise) and a
  deviation that gains more than ε from it (`deviation_states`). The
  notion is weaker than subgame perfection, which stays undefined
  (I.7). `concept: subgame_perfect` and `correlated` are reserved
  (I-11 information and I-9 error); any other value is an I-9 error.
  The IR of I.13 has a field `concept` on every entry of `equilibria`.
- `tests/test_games_next.py` and `tests/data/games_next/`: fixed files
  on which each new soundness mutant is detected deterministically (a
  gain with a hump in a weight and in the probabilities, a gain that is
  convex but not monotone, a state reached only by the player's own
  deviation with probability p, and an entry-deterrence game whose
  threat is not credible), and fuzzing with 3 and 4 players, 3 open
  parameters and the new concept (`tests/games_fuzz.py --mode scale`,
  `--mode endpoints`, `--concept every|both`).
  Two files of the independent review (`bstate.cadl`: condition B must
  be checked per state; `ns_affine_zero.cadl`: the cell at p = 0 stays
  undecided) are tests too, with the mutant `endpoints_b_from_initial`.
- The end-point certification of a cell (formerly J-14 of Appendix J)
  is a method of I.9 and is used by `cadl game-check --certify` for any
  strategic model, also without `grounding: 0.1`, on the cells that the
  conditions N, E1 and E2 leave undecided; every claim of those
  conditions is kept. Its conditions are stated in terms of Appendix I:
  A (one open parameter ℓ, in no probability and no reward value, with a
  power of at most one in every weight, counted on the text) and, for a
  deviator, B− or B+ (every term of the player whose weight has a
  non-zero coefficient of ℓ at the fixed values of the other parameters
  has a coefficient of one sign and the expected value 0 under the
  profile; the gain is then non-increasing, resp. non-decreasing, so the
  gain at b, resp. a, decides). A constant part of a weight, such as
  `1 - 2 * lambda`, no longer prevents a claim, and B+ is new. J-14 now
  refers to the method of I.9. In the output, the map field `j14` is
  replaced by `endpoints` (`applicable`, `parameter`, `non_increasing`,
  `non_decreasing`, or `reason`), the text line "J-14 applies ..." by
  "end points (I.9): ...", and the cell method `endpoints (J-14)` by
  `end points (I.9)`. On the bundled examples every claim of 0.3.23 is
  unchanged.

## [0.3.23] — 2026-11-11

Two new extensions and an extension of the norms engine. The strategic
model extension (`games` 0.1, Appendix I) reads the model of Appendix H
as a turn-based stochastic game and decides whether a named profile,
such as compliance, is an equilibrium, also as a certified map over
parameters (`cadl game-check`). The grounding extension (`grounding`
0.1, Appendix J) derives indices of information sharing, concentration
of powers and incentive coverage from the norms of a contract, compares
them with the declared α and β (`cadl norms-indices`), and links the
sanction of a norm to a payoff term weighted by λ. Quantified predicates
and aggregates are now evaluated by the norms engine, the generated
monitor and the agent runtime. Each part was reviewed independently
before the release; see the review histories in Appendices I, J and F.5.

Known limits: the strategic export targets PRISM-games and was not run
through it here; the assumption that a reward structure counts the
violations of a norm cannot be checked and is reported with every
result that relies on it; J-14 is available only in files that declare
`grounding`; `[*]` inside a predicate is not evaluated.

### Added
- Quantified predicates and aggregates in the dynamic semantics of the
  norms extension (Appendix F, F.5, "Quantifiers and aggregates"):
  `cadl norms-eval`, the generated `norms_monitor.py` and `cadl
  agents-run` evaluate `exists x in D: p`, `for all x in D: p` and
  `sum` / `count` / `min` / `max` / `avg` of a comprehension. Up to
  0.3.22 a norm that used one was reported as not evaluable and never
  judged. The domain is a declared set (`A`, `A[*]`, `A[lo..hi]`, for a
  comprehension also `lo..hi`), and the variable is the index of a
  member. With integer bounds the domain is closed (Kleene's logic). A
  symbolic bound has a value when the instance binds it or it is a
  quantity with an integer value at that observation; otherwise the
  domain is open, its known members are those the instance has named so
  far, `exists` is true only by a known witness, `for all` false only by
  a known counterexample, and every other value and every aggregate is
  undefined. The value at an observation depends only on the prefix up
  to it. In the agent runtime an undefined quantified `trigger:` or
  `until:` is missing data (G.5); the limit of G.13 that a prohibition
  with a quantified trigger always applies is gone for quantifiers. A
  wildcard or range reference outside a domain (`ROBOT[*].battery > 10`)
  is still not evaluated. A closed domain is enumerated up to 10 000
  members (`MAX_DOMAIN`); a larger one, which a single value of `N` in a
  trace could ask for, is evaluated as an open domain with a warning. A
  domain stays open while a member beyond the value of its bound has
  been named (F.3 makes it a member). A quantifier variable never hides
  a declared symbolic bound, and a reference such as `R[j+1]` with an
  index outside the set is undefined.
- `examples/norms_no_double_assignment.cadl` with three traces: the same
  delivery must not be assigned to two robots at once.
- `cadl check` warns about a prohibition whose message `trigger:` is
  also an occurrence of its message `content:`, which violates it at the
  observation that activates it (N-13; also through an institutional
  event that a counts_as rule creates from the trigger message, and
  with arguments compared by value), and about a quantifier or
  comprehension variable named like a symbolic bound of a declared set
  (N-14).
- **Strategic model extension (`games` 0.1, Appendix I).** A file that
  declares `stochastic: 0.1` and `games: 0.1` may give its `model:` block
  `kind: smg`: a turn-based stochastic game on the model of Appendix H,
  with `players:`, `owner:` on a command, named profiles of memoryless
  deterministic strategies (`profiles:`, `compliance:`), payoffs as signed
  weighted sums of expected rewards accumulated until a target
  (`payoffs:`), and equilibrium queries (`equilibria:`).
  - `cadl check` applies the rules I-1 to I-11 of I.10, among them I-5
    (at most one player chooses in a state; it replaces S-9), I-6 (every
    profile selects a command wherever there is a choice), I-8 (every
    profile is admissible at the fixed values: no single player can keep
    the game away from the target of its payoff) and I-9 (for a map: the
    design is well-defined at the corners of the region and the profile
    admissible at the centre of each of its 3ⁿ faces).
  - `cadl game-check FILE [--equilibrium ID] [--regime R] [--set p=v]`:
    the **equilibrium checks** of I.8 at the fixed values, with exact
    rational arithmetic: per player the payoff, the best-response value
    (policy iteration with exact linear solves), the deviation gain and
    the canonical best response; `status`, `deviators`, and
    `from_every_state`.
  - `cadl game-check FILE --certify [--max-depth N] [--max-cells N]
    [--partition N] [--no-merge]`: the **equilibrium maps** of I.9. A cell
    is `equilibrium` or `not_equilibrium` (with `deviators`, a deviation
    for each, and bounds of the gains) only when that holds at every
    valuation of the cell, established by parameter lifting of the game
    of each player against the profile and the conditions N, E1 and E2;
    otherwise `undecided`, with a `reason`. Text and JSON output in the
    shape of I.13; exit status 0 (computed), 1 (a map did not reach its
    resolution, or a query was not evaluated), 2 (nothing can be claimed).
  - `cadl model-check` evaluates the properties and regime maps of a
    strategic model in the Markov chain of their `profile:` (default: the
    compliance profile). `cadl model-export` writes the game in the model
    language of PRISM-games (`smg`, `player … endplayer`; no properties
    file; not run through PRISM-games). `cadl sim-ir` carries the game in
    `stochastic_model` (`kind: "smg"`, `owner`, `profile`,
    `open_in_equilibria`, `players`, `profiles`, `compliance`, `payoffs`,
    `equilibria`).
  - A payoff term with `sanction_of:` or `reward_of:` (Appendix J) is
    parsed and never read with a default weight: the equilibrium queries
    of such a model report `not_evaluated`.
  - Examples `examples/games_robot_delivery.cadl` and
    `examples/games_two_players.cadl` (the two examples of I.12).
  - Tests: every number of I.12; a soundness fuzzer of the maps
    (`tests/games_fuzz.py`: random parametric games against a brute-force
    oracle that shares no code with `cadl`), with five deliberate
    soundness bugs that it must detect; best-response values compared with
    Storm (exact) when `stormpy` is installed.
- **Grounding extension (Appendix J, grounding 0.1), the norms side.** A
  file that declares `grounding: 0.1` together with `norms: 0.1` may use
  `grounding: {items, tolerance}` in a contract, `shares:` and
  `on_fulfilment: {reward, magnitude}` in a norm, and `magnitude:` in
  `on_violation:`. The keys are annotations: `cadl norms-eval`,
  `cadl agents-run` and the generated `norms_monitor.py` give the same
  results with and without them, and no code generator writes them.
- `cadl norms-indices FILE [--contract ID] [--format text|json]`: for
  every contract, the derived index of information sharing (alpha^), the
  derived index of the concentration of powers (beta^) and the incentive
  coverage (chi) of J.4, as exact ratios or as undefined with the reason,
  with the counts J-5 asks for: the pairs given by a sharing entry, an
  obligation and a permission, the pairs not given, the member-specific
  entries and norms, the powers per bearer, the norms in the effects of
  each power, the share of the decision holder, and the norms with a
  reparation only and with a transition.
- `cadl check`: the rules J-1 to J-9 of J.8 (README, Type Checks, item
  15). J-1 to J-4 are errors and concern the new keys only; every
  comparison of a declared `alpha`, `beta` or `lambda` with the norms
  (J-6 to J-8) is a warning, and J-9 is informational. The comparisons
  are exact; J-7 does not apply to a declared end point (0 or 1), which
  J-6 tests, and J-9 does not repeat a declared value that J-6 or J-7
  reports. A sharing entry with more than one item or with arguments
  gives no pair (warning). A text of white space only counts as empty.
  Declaring `grounding: 0.1` in a file that declares
  `norms: 0.1` and uses none of the new keys adds no error. The new keys
  in a file that does not declare the extension get one warning as
  undeclared extension keys and are not interpreted (J.12).
- `cadl norms-diff`: for a file that declares the extension, changes of
  `on_violation.magnitude`, `on_fulfilment:`, `shares:`,
  `grounding.items`, `grounding.tolerance` and of each index, and a
  warning when a declared `alpha` or `beta` and its index move in opposite
  directions between the two versions (J-13). A norm added in the new
  version is listed with its magnitude, `on_fulfilment:` and `shares:`;
  when only one version declares the extension, the report says that the
  indices are not compared.
- IR (`cadl sim-ir`, J.10), only for a file that declares the extension:
  `grounding` in every contract (`items`, `tolerance`, `indices`) and
  `shares`, `on_violation_magnitude`, `on_fulfilment_reward`,
  `on_fulfilment_magnitude` in every norm. The IR of every other file is
  unchanged.
- `cadl.grounding.norm_payoff_link`, `covers` and `lambda_parameters`:
  what a payoff term `sanction_of:` / `reward_of:` of a strategic model
  (Appendix I; J.7, J-10) needs from the norm it names.
- **The link between the strategic model and the norms (J.7).** In a
  file that declares `stochastic`, `games`, `norms` and `grounding`, a
  payoff term `sanction_of: "C.n"` has the weight −m · ℓ and a term
  `reward_of: "C.n"` the weight m′ · ℓ (m, m′ the magnitudes of the
  norm, ℓ the parameter bound to `C.lambda`). `cadl check` applies J-10
  (errors), J-11 (warnings) and J-12 (the expected count of each
  `sanction_of:` and `reward_of:` term under the compliance profile, per
  design; a `reward_of:` term always breaks condition (ii) of J.7). Every result of
  `cadl game-check` computed with such a term carries the assumption of
  J.7 (`assumptions` in JSON). The IR writes the defined weight and the
  fields `sanction_of` / `reward_of`.
- **J-14** in `cadl game-check --certify`: in a file that declares
  `grounding: 0.1`, a one-parameter equilibrium map whose parameter
  (bound to `C.lambda` or not) occurs only in weights, with a power of at
  most one counted on the text (no cancellation), decides a cell
  that the bounds of I.9 leave undecided from exact evaluations at its
  end points (`method: "endpoints (J-14)"`; deviators only under
  condition (ii) of J.7). Every map reports whether J-14 applies, or why
  not, also in a file without the grounding extension. The fuzzer (`tests/games_fuzz.py --j14`) checks
  these cells and detects three deliberate violations of the conditions.
- `examples/games_grounding_robot_delivery.cadl`: the example of J.7.
- `examples/grounding_robot_delivery.cadl`: the example of J.9
  (alpha^ = 5/8, beta^ = 1/2, chi = 1/3).

### Changed
- `cadl model-export` of a strategic model renames also the keywords that
  PRISM-games adds (`player`, `endplayer`, `smg`, `csg`, `tptg`) by the
  rule of H.11, and reports two players that are written the same (I.11).
- `cadl game-check` reports a query that is not evaluated in the shape of
  I.13: per design `{design, status: "not_evaluated", reason}`.
- In a file that declares `stochastic: 0.1` and not `games: 0.1`, a
  `model:` block that uses `kind: smg` or keys of the strategic model
  extension gets one more informational diagnostic (the keys belong to an
  undeclared extension, I.15). It is read by Appendix H as before. Models
  of Appendix H are evaluated, exported and lowered exactly as in 0.3.22.

## [0.3.22] — 2026-11-11

Three parts of the extension appendices that were specified but not
implemented: certified regime maps (Appendix H), a stateful runtime for
agent governance (Appendix G), and an online monitor of norms in the
generated Python code (Appendix F). Each was reviewed independently
before the release; the agent runtime went through seven review rounds,
and the defects they found are fixed here. `cadl check` gains the rules
G-17 to G-19 for enforced contracts, and `cadl norms-eval` reads traces
more strictly (see Changed).

Known limits of the agent runtime are listed in G.13 of the
specification: it is an offline replay of a recorded trace without a
protocol adapter; the audit log has no integrity protection; a predicate
with a quantifier or `[*]` is not evaluated, so a prohibition that
depends on it always applies and a permission never does; amendments were
tested by hand only; the Cedar policies cover static norms and were not
part of these reviews.

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
  A probability that is not multi-affine and leaves [0, 1] inside the
  region, where the corner check of S-14 in `cadl check` cannot see it,
  is reported as an error (exit status 2) when the centre or a corner of
  a cell shows it.
- Tests: every `decided` and `infeasible` cell of eight maps is checked
  at sample points with the exact point evaluator, and, when `stormpy`
  is installed, the bounds are compared with Storm's parameter lifting
  on the exported files. New test model
  `tests/data/stochastic_regions.cadl`.
- `cadl codegen -t python`: for a file that declares `norms: 0.1` the
  generated package has a new module `norms_monitor.py`, an online
  monitor of the norms extension (Appendix F, F.4 and F.5). A running
  system makes a `NormsMonitor` for a contract instance, feeds it one
  observation at a time (`observe`, with the content of one trace line
  of `cadl norms-eval`) and gets back the changes of that observation:
  which norm instance, guarantee or assumption changed to which state
  and verdict (`pending`, `fulfilled`, `violated`, `excused`), with time
  and reason. `end()` settles the norms, `verdicts()` and `result()`
  give the current state, and `next_deadline()` the next time a deadline
  of a norm is over. The module needs only the standard library (Python
  3.9 or later) and does not import cadl. It holds the contracts as data
  and a verbatim copy of the engine of `cadl norms-eval`, so there is one
  implementation of F.5; `python -m PACKAGE.norms_monitor TRACE.jsonl`
  prints what `cadl norms-eval --format json` prints, without the key
  `file`. Amendments (F.6) work when the model of the new version is
  given (`versions=`, `--model-version NAME=MODEL.json`). `next_check()`
  gives the earliest time stamp at which an observation is certain to
  decide a deadline or a temporal operator that only waits for time. A
  refused observation (`TraceError`, or `ObservationError` for an
  amendment that cannot be applied) leaves the monitor as it was. The
  model is embedded as one JSON text, and the generator compiles the
  module and checks that it defines every name it uses before it writes
  anything. See "Norms Monitor in Generated Python Code" in the README,
  also for what an online monitor cannot know.
- The generated package of such a file can be imported where cadl is not
  installed: `NormsMonitor` works, and the other names, which need
  `cadl.codegen.runtime_support`, raise the import error when used.
- `cadl agents-run FILE --trace TRACE [--format text|json] [--audit OUT.jsonl]`
  and `cadl.agents_runtime.run`: the reference runtime of the agent
  governance extension (Appendix G, G.5 to G.8). It replays a recorded
  sequence of messages through one logical enforcement point over the
  enforced contracts of a file. For every message it derives the state
  that the decision function needs (the in-force flags of the norms that
  are not static from the verdict engine of `cadl norms-eval`, the valid
  derived instances from the grants and revocations seen so far, the
  running contract instances and their bindings), calls the decision
  function of `cadl agents-decide`, and applies the result: an allowed
  message becomes an observation of the norms engine, a denied or held
  message does not. An `escalate` creates a hold (`h-1`, `h-2`, ...); an
  `approval` or `rejection` of the approver with the hold id as first
  argument, the timeout, the end of a contract instance or an amendment
  closes it; an approved message is decided again at the time of the
  approval and, if nothing denies it, released as the next observation; it
  does not violate its grounds (G.6). The grounds of a hold are kept per
  contract instance. In the second decision the approval stands in for
  the permission and for the grounds only there: a prohibition that
  applies in another contract instance, an ended contract instance of a
  ground, or another governing contract that does not allow the message
  any more (a revoked delegation, an ended instance, an amendment) denies
  the release. An instance of an enforced contract observes a message
  only when its contract governs it. Grants and
  revocations keep the derived instances with their provenance chains,
  `max_depth`, `to:` and `until:` (G.7). An obligation whose content the
  contract itself denied or held is `excused`, and a violated prohibition
  stays in force as G.5 says. The runtime is deterministic and offline:
  its only clock is `t` of the trace.
- Trace format of `cadl agents-run`: the JSON Lines of `cadl norms-eval`
  for the whole enforcement point, with `epoch` (first line: the time of
  `t = 0`, so that audit times are ISO 8601 time stamps) and `instances`
  (host-created contract instances with `bindings`, `request`, `state`
  and `end`).
- Audit log (G.8), in the JSON output and as JSON Lines with `--audit`:
  decision records with exactly the fields of G.8 (`time`, `message`,
  `instances`, the result of G.10, `hold_id`, `outcome`), two per hold,
  and event records (`event`, `norm`, `chain`, `holders`, `hold_id`,
  `reason`) for grants, revocations, unauthorised acts, the end of a
  derived instance for any other cause, the start and the end of a
  contract instance, and an amendment. A closing record lists the contract
  instances of the grounds of its hold. The log is reproducible; it is
  written only when the whole trace was replayed; it has no hash chain or
  signature, which G.8 does not ask for.
- `cadl.agents.applying_prohibitions`, `governs` and `message_kind`: parts
  of the decision function that the runtime needs on their own. `decide`
  uses them and returns what it returned before.
- `examples/agents_change_control.cadl` (a prohibition in force between a
  trigger and an `until:`, a delegation with `max_depth: 1` and `until:`)
  and five traces under `examples/traces/` for it and for
  `examples/agents_tool_governance.cadl`.

- `cadl agents-run`: every contract instance must bind the index
  variables of the parties of its contract (`bindings`); an instance
  without such a binding, or with a value that is not the index of a
  member of the set the party names, is a trace error; a binding of a
  variable that no party has is ignored with a warning; a value must be
  a member of every declared set that the variable indexes in the
  contract. Every decision depends only on the lines up to it: a bare
  name of a predicate is a quantity from the first line on that gives it
  a value (a symbolic value before), or from the start when the first
  line names it under `quantities`; nothing after the end of an instance
  changes its verdicts. Whether a contract has host-created instances is
  fixed on the first line (`instances` there, or the key
  `host_instances`); a later entry that creates an instance of another
  contract is a trace error. Missing data is unknown without latching:
  while the predicate `trigger:` of an inactive prohibition, or the
  predicate `until:` of an active permission, is undefined for want of a
  value, the in-force flag is left out of the policy input, so the
  prohibition applies and the permission does not; the same when that
  `trigger:` or `until:` is an institutional event that a `counts_as:`
  rule creates from a predicate; the norm states stay those of
  `cadl norms-eval`.
- `cadl check`, G-19 (warning): an institutional event that a rule
  creates under an `if:` and that is the trigger of a prohibition or the
  `until:` of a permission of an enforced contract. A message that
  arrives while the `if:` has no value creates no event; `cadl agents-run`
  warns when that happens. `cadl.agents_runtime.run`
  refuses a file with errors of `cadl check`; `check=False` is an unsafe
  switch for tests. As defence in depth, an occurrence that depends on an
  unbound index is unknown and never has an effect that lets a message
  through (activate or resume a permission, end, suspend or discharge a
  prohibition, create an institutional event, exercise a power, fulfil an
  obligation), and always has the effects that block.

### Changed
- `cadl codegen`: the warning for an `assume:` / `guarantee:` item with a
  temporal operator says, for the Python target, that the item is left
  out of `contracts.py` and monitored by `norms_monitor.py`, and for the
  other targets that only the Python target generates a monitor for it.
  `contracts.py` has a second comment line under each left-out item.
- `cadl/norms_eval.py`: the engine reaches the parser through three
  functions, reads a trace one observation at a time (`_TraceReader`,
  which checks an observation completely before it keeps anything of it)
  and processes one observation per call (`_Engine._step`), so that the
  generated monitor can use the same code. `run_trace` is what `evaluate`
  returns. `select_contract` has a third, optional argument for the
  wording of its error message.
- `cadl.norms_eval`: the engine can be driven one observation at a time
  (`_Engine._step`), and a norm can be exempted from the content of one
  observation; `cadl agents-run` uses both. `cadl norms-eval` and its
  output are unchanged.
- `cadl norms-eval`: `from`, `to` and `name` of a message of a trace must
  be written exactly: a concrete actor (`ROBOT[2]`) and an identifier.
  Whitespace inside them is no longer removed, so a trace with
  `"ROBOT[ 2 ]"` or `"ROB OT"`, which 0.3.21 read as `ROBOT[2]` and
  `ROBOT`, is now a trace error. This is deliberate: two spellings of one
  actor in a log are a way to slip past a check that compares names.
- `cadl check`, new rules for an enforced contract (Appendix G): G-17,
  the sender and the receiver of every message event of the contract
  (`trigger:`, `until:` and `content:` of its norms, `when:` of its
  `counts_as:` rules, `until:` of a delegation) must be covered by its
  parties, by one reference of `parties:`, and must name only declared
  members (`SUP[7]` for a set `SUP[1..3]` is an error); G-18, every index
  variable of the contract (in bearers, counterparties, `escalate_to:`,
  `notify:`, `fallback:`, the senders, receivers and arguments of its
  message events, and its predicates, also inside an index expression
  such as `ROBOT[j+1]`) must be bound: by a party (`i` with a party
  `ROBOT[i]`), by a quantifier, or by a comprehension.
  Both are errors. A contract with parties `ROBOT[*]` and norms of
  `ROBOT[i]` has to write the party as `ROBOT[i]`. A file that 0.3.21
  accepted with such an event has to name the actor as a party. For
  the rules that create `approval` and `rejection`, G-17 makes sure that
  the contract governs the approver's message.

### Fixed
- `cadl model-export`: a transition reward whose action label no command
  of the design has (a command with `when:` in another regime) is no
  longer written. PRISM 4.11 and Storm reject a reward item that names an
  action the model does not have, so such a file did not load. A reward
  structure left without items gets the item `true : (0);`, because Storm
  rejects an empty structure.
- `cadl norms-eval`: an actor reference in a `trigger:`, an `until:`, the
  `when:` of a `counts_as:` rule or a message content matched too many
  actors. A range (`ROBOT[1..2]`) was read as `ROBOT[*]` and matched
  `ROBOT[3]`; `ROBOT[*]` and an index that the instance does not bind
  matched any actor of that name, also outside the declared set
  (`ROBOT[9]` for `ROBOT[1..3]`). A reference now matches as F.3 of the
  specification says: a range only its members, `A[*]` and an unbound
  `A[i]` only members of the declared set, an undeclared actor never. The
  verdicts on the six bundled traces are unchanged.
- `cadl norms-eval`: an observation after the end of the contract
  instance has no effect, as F.5 says. Before, the names of the observed
  quantities were also taken from such observations, so that a name that
  got its first value after the end was undefined instead of a symbolic
  value during the instance, and a verdict could differ from that of the
  same trace without those lines. The result of a trace without
  observations after the end is unchanged. A `state` after the end no
  longer produces the warning about a contract without `lifecycle:`.
- `cadl norms-eval`: a time `t` written as an integer too large for a
  floating-point number (more than about 300 digits) is rejected like
  every time above 2**53 s, instead of ending in an internal error.
- A number in a predicate that overflows to infinity (`1e999`) is no
  longer read as infinity: the text is not a predicate (`cadl check`
  warns that it is kept as text; a norm with it is not evaluated).
- `cadl norms-eval` and `cadl agents-decide`: a set declared with a
  symbolic upper bound (`R[1..N]`) has every index from its lower bound
  on; `R[0]` was taken as a member. The Rego policy of `-t agent-rego`
  tests the lower bound too: regenerate it.
- `cadl norms-eval`: a trace is rejected (exit status 2) when `t` is
  `NaN`, infinite or larger than 2**53 in absolute value, or when another
  number of a line is `NaN` or infinite. Epoch seconds and epoch
  milliseconds are within the bound.

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
