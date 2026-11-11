# CADL: Contract Architecture Description Language

English | [日本語](README_ja.md)

CADL is a domain-specific language for formally describing, verifying, and deploying institutional designs in Systems of Systems (SoS). It enables stakeholders — from engineers to citizens — to define governance rules, contracts, protocols, and incentive structures in a machine-readable, verifiable format.

CADL is developed at ERTL, Graduate School of Informatics, Nagoya University.

**Documentation**

- [Specification and language reference](https://www.ertl.jp/cadl-spec/) ([日本語](https://www.ertl.jp/cadl-spec/ja/)) — start with [Chapter 5](https://www.ertl.jp/cadl-spec/docs/spec/language-spec) for the language and [Appendix A](https://www.ertl.jp/cadl-spec/docs/spec/appendix-a-syntax) for the complete syntax
- [Hands-on course](https://www.ertl.jp/cadl-spec/docs/handson/) — a guided walkthrough of the toolchain
- This README — installation, commands, and the bundled examples
- [CHANGELOG](CHANGELOG.md) · [Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md) · [Issues](https://github.com/ertlnagoya/cadl/issues)

## Motivation

In SoS environments, independently operated systems must coordinate under shared rules. Today, these rules exist mostly as natural-language contracts and tacit agreements, making them prone to ambiguity, contradiction, and drift between design intent and implementation.

CADL addresses three core goals:

1. **Make institutions computable** — Model real-world rules (authority structures, information-sharing policies, incentive mechanisms) as first-class data objects that can be copied, composed, versioned, and searched.
2. **Detect contradictions and violations** — Enable automated consistency checking (via SMT solvers and model checkers) and runtime monitoring of institutional constraints.
3. **Generate executable artifacts** — Translate verified institutional designs into control logic, monitoring code, and smart contracts for target platforms.

## Language Overview

A CADL file (`.cadl`) uses a YAML-like declarative syntax. Each file describes one SoS definition containing:

| Section | Purpose |
|---|---|
| `actors` | Constituent systems with roles, autonomy levels, and capabilities |
| `contracts` | Assume-guarantee contracts with authority (beta), information sharing (alpha), and incentive (lambda) parameters |
| `protocols` | Coordination procedures with message passing, timing constraints, and fallback behaviors |
| `algorithms` | References to central/local algorithms |
| `transitions` | Regime transitions with safety invariants |
| `metrics` | Performance indicators with formulas and targets |
| `verification` | Properties to check, with the method to use (only `smt` is implemented) |

A contract may also carry `lifecycle:` and `monitors:` blocks (the SoS-DSL extension), which describe the states a contract instance goes through and the conditions watched while it runs. The complete list of keys is in [Appendix A](https://www.ertl.jp/cadl-spec/docs/spec/appendix-a-syntax) and [Appendix E](https://www.ertl.jp/cadl-spec/docs/spec/appendix-e-sos-dsl) of the specification.

### Institutional Parameters

CADL quantifies institutional characteristics using three continuous parameters in [0, 1]:

- **alpha** — Information sharing degree (0 = local only, 1 = full sharing)
- **beta** — Decision centralization (0 = fully distributed, 1 = centralized)
- **lambda** — Incentive strength (0 = directive-based, 1 = market mechanism)

### Example

```yaml
sos:
  name: "RobotDeliverySystem"
  type: Acknowledged
  version: "1.0.0"

  actors:
    - id: DISPATCHER
      role: "global_planner"
      autonomy: low
    - id: "ROBOT[1..N]"
      role: "delivery_vehicle"
      autonomy: high

  contracts:
    - id: DELIVERY_SLA
      parties:
        - DISPATCHER
        - "ROBOT[*]"
      assume:
        - "DISPATCHER.is_operational == true"
        - "network_latency <= 200ms"
      guarantee:
        - "all_routes_conflict_free()"
        - "delivery_time <= promised_time * 1.2"
      authority:
        decision_holder: DISPATCHER
        beta: 0.8
      information:
        alpha: 0.8
      incentives:
        type: reputation
        lambda: 0.5
        rules:
          - "reward(ROBOT[i], 10) when on_time_delivery"
          - "penalty(ROBOT[i], -5) when route_deviation"
      duration: indefinite

  protocols:
    - id: FAILURE_REPLAN
      trigger: "road_failure_detected_by(ROBOT[i])"
      steps:
        - "ROBOT[i] -> DISPATCHER : failure_report"
        - "DISPATCHER : recompute_routes"
        - "DISPATCHER -> ROBOT[*] : new_route"
      timing:
        max_total: 500ms
      postcondition: "all_routes_conflict_free()"

  transitions:
    - from: IDLE
      to: ACTIVE
      condition: "delivery_request_pending == true"
      safety_invariant: "all_routes_conflict_free()"
    - from: ACTIVE
      to: IDLE
      condition: "all_deliveries_complete == true"
      safety_invariant: "all_robots_at_base == true"
```

## Installation

Requires Python 3.9+.

```bash
pip install cadl-lang

# With AI features (requires Anthropic API key)
pip install "cadl-lang[ai]"
```

The distribution is named `cadl-lang` on PyPI; the import package and the command are both `cadl`.

To work on CADL itself, install from source:

```bash
git clone https://github.com/ertlnagoya/cadl
cd cadl
pip install -e ".[dev]"
pytest
```

## Usage

```bash
# Parse a CADL file and display summary
cadl parse examples/robot_delivery.cadl

# Parse and run type checks
cadl check examples/robot_delivery.cadl

# Full verification (type check + SMT + deadlock detection)
cadl verify examples/robot_delivery.cadl

# Generate Python runtime code
cadl codegen examples/robot_delivery.cadl -o /tmp/robot_delivery

# Generate Solidity smart contracts
cadl codegen examples/robot_delivery.cadl --target solidity -o /tmp/solidity_out

# Generate OPA/Rego policies
cadl codegen examples/robot_delivery.cadl --target opa -o /tmp/rego_out

# Generate Unity C# for SoS-DSL contracts (lifecycle / monitors)
cadl codegen examples/sos_dsl_robot_delivery.cadl --target unity-csharp -o /tmp/unity_out

# Agent governance extension (agents 0.1): decide one message at the boundary,
# replay a recorded sequence of messages (holds, delegations, audit records),
# and generate the boundary policy as Rego or Cedar
cadl agents-decide examples/agents_tool_governance.cadl --input examples/agents_inputs/delete_file.json
cadl agents-run examples/agents_tool_governance.cadl --trace examples/traces/agents_tool_governance_a_approved.jsonl
cadl codegen examples/agents_tool_governance.cadl --target agent-rego -o /tmp/agent_rego
cadl codegen examples/agents_tool_governance.cadl --target agent-cedar -o /tmp/agent_cedar

# Analyze regime transitions
cadl regime-map examples/smart_city_traffic.cadl
cadl regime-map examples/smart_city_traffic.cadl --format dot -o regime.dot

# IEC 62853-oriented dependability summary
cadl iec62853 examples/robot_delivery.cadl
cadl iec62853 examples/smart_city_traffic.cadl --format json

# Verdicts of the norms extension on a recorded trace
cadl norms-eval examples/norms_robot_delivery.cadl --trace examples/traces/norms_robot_delivery_a_fulfilled.jsonl

# The same verdicts from the monitor in the generated Python package (it runs without cadl)
cadl codegen examples/norms_robot_delivery.cadl -o /tmp/gen/robot_delivery_norms
(cd /tmp/gen && python -m robot_delivery_norms.norms_monitor "$OLDPWD/examples/traces/norms_robot_delivery_a_fulfilled.jsonl" --format text)

# What an amendment from one version to the next does to the norms
cadl norms-diff examples/norms_amendment_v1.cadl examples/norms_amendment_v2.cadl

# Grounding extension: the derived indices of information sharing and of the
# concentration of powers, and the incentive coverage, of each contract
cadl norms-indices examples/grounding_robot_delivery.cadl
cadl norms-indices examples/grounding_robot_delivery.cadl --format json
# Stochastic model extension: export the model: block to PRISM, one model
# file and one properties file per design
cadl model-export examples/stochastic_robot_delivery.cadl -o prism_out/

# Evaluate its properties with the internal reference evaluator (exact arithmetic)
cadl model-check examples/stochastic_robot_delivery.cadl
cadl model-check examples/stochastic_robot_delivery.cadl --regime-map best_for_delivery --grid 5

# Certified regime map: which regime is best in which cell of the parameter space
cadl model-check examples/stochastic_robot_delivery.cadl --regime-map best_for_delivery --certify

# Strategic model extension (games 0.1): is the compliance profile a Nash
# equilibrium, and in which cells of a parameter region is it one?
cadl game-check examples/games_robot_delivery.cadl
cadl game-check examples/games_robot_delivery.cadl --certify --equilibrium comply_by_lambda

# Generate CADL from natural language (requires ANTHROPIC_API_KEY)
cadl ai "A fleet of 5 autonomous drones surveying farmland, coordinated by a ground station"
cadl ai -f requirements.txt -o output.cadl
```

## Architecture

The CADL toolchain follows a standard compiler pipeline:

```
.cadl file
    |
    v
+------------------+
|  Parser          |  YAML structure + Lark expression grammar
|  (parser.py)     |  -> Abstract Syntax Tree
+--------+---------+
         |
         v
+------------------+
|  Type Checker    |  Actor reference resolution, parameter range
|  (type_checker)  |  validation, contract party consistency
+--------+---------+
         |
         v
   [Checked AST]
         |
    +----+----+----------+-----------+
    v         v          v           v
  SMT       Code       Regime      IEC 62853
  Verify    Generate   Map         Summary
  (Z3)      (codegen/) (regime_map) (iec62853)
              |
        +-----+-----+--------+
        v     v     v        v
     Python Solidity OPA/Rego Unity C#
```

### Code Generation

`cadl codegen` generates executable code from a CADL definition. It runs the type check but not the verifier, so run `cadl verify` first. Four targets are supported:

**Python** (`--target python`, default):

| Generated Module | Content |
|---|---|
| `actors.py` | Actor base classes with role, autonomy, and capability stubs |
| `contracts.py` | Contract monitor classes with assume/guarantee checks |
| `protocols.py` | Async protocol state machines with timeout handling |
| `transitions.py` | Regime controller with transition conditions |
| `metrics.py` | Metrics collector with formula computation |
| `runtime.py` | Top-level orchestrator wiring all components |
| `norms_monitor.py` | Only for a file that declares `norms: 0.1`: an online monitor of the norms, counts-as rules and temporal items of its contracts ([below](#norms-monitor-in-generated-python-code)) |

Generated code inherits from base classes in `cadl.codegen.runtime_support`; `norms_monitor.py` does not import cadl.

**Solidity** (`--target solidity`):

Generates Ethereum smart contracts (`.sol`):
- One contract per CADL `ContractDef` with `checkAssumptions()`, `checkGuarantees()`, `runMonitorCycle()`
- `RegimeController.sol` with enum-based state machine for transitions
- Main orchestrator contract importing all sub-contracts
- Institutional parameters (alpha, beta, lambda) as scaled `uint256` constants

**OPA/Rego** (`--target opa`):

Generates Open Policy Agent policies (`.rego`):
- One package per CADL `ContractDef` with `assumptions_hold`, `guarantees_hold`, `allow`, and `violation` rules
- Main policy package aggregating all contracts
- Information sharing and authority policy rules

**Unity C#** (`--target unity-csharp`):

Generates C# for contracts that use the SoS-DSL extension (`lifecycle:` / `monitors:`):
- `Generated/<Contract>State.cs`, `<Contract>Contract.cs`, `<Contract>Monitors.cs` per contract
- `Runtime/` support classes (contract runtime, event bus, predicate evaluator)

[Chapter 7 of the textbook](https://www.ertl.jp/cadl-spec/docs/textbook/codegen-runtime/) runs this target end to end.

**Agent boundary policy** (`--target agent-rego`, `--target agent-cedar`):

Generates the boundary policy of the agent governance extension (see below) from the enforced contracts of a file that declares `agents: 0.1`; without the declaration `cadl codegen` reports an error. Both are independent of the `opa` target.
- `agent-rego` writes `agent_policy.rego`, one Rego package (`cadl.agent_policy`) that takes the policy input as `input` and returns the whole result object as `result` (and `decision`), for static and non-static norms alike; the in-force flags of non-static norms and the derived instances are read from `input.instances`. The header lists the norms that are not static
- `agent-cedar` writes `agent_policy.cedar` and `agent_policy_table.json`. The policies cover the statically decidable part: `forbid` for undeclared tools and static prohibitions, `permit` for static permissions, and Cedar's default denial for tool calls without a permission. Cedar has two outcomes, so an escalation is a denial that the runtime resolves from the table (per prohibition: `escalate_to`, `timeout`, `fallback`, `notify`). The README comment at the top of the file states what is not covered (non-static norms, derived permissions, contract instances); no policy reads an attribute, so none can be skipped because of an evaluation error

### Agent Governance (agents 0.1)

The agent governance extension (Appendix G of the specification) builds on the norms extension. A file declares both `norms: 0.1` and `agents: 0.1`; then `agent:` binds an actor to a kind (`llm_agent`, `tool_server`, `human`, `service`), a protocol, and for a tool server its `tools:`; `enforcement:` makes a contract enforced; `on_denied:` on a prohibition names an approver (`escalate_to:`), a `timeout:`, a `fallback:` message, and an actor to `notify:`; and `delegations:` makes a permission delegable. `examples/agents_tool_governance.cadl` is the example of the appendix.

`cadl agents-decide <file.cadl> --input <input.json> [--format text|json]` is the reference decision function (G.5): for one message it returns `allow`, `deny`, or `escalate`, with the reason, the determining contracts, the norms, the provenance chains of derived permissions, and the escalation data.

```bash
$ cadl agents-decide examples/agents_tool_governance.cadl --input examples/agents_inputs/delete_file.json
decision:    escalate
reason:      prohibition
contracts:   ASSISTED_REPORTING
norms:       ASSISTED_REPORTING.no_delete
provenance:  -
escalate_to: OPERATOR
timeout:     600000 ms
fallback:    PLANNER -> TICKETS : create_ticket
notify:      OPERATOR
```

The input is a JSON object with `message` (`from`, `to`, `name`) and optionally `now` and `instances` (per running contract instance: `bindings`, `in_force` flags of the norms that are not static, and the valid `derived` instances with their chains); `examples/agents_inputs/` has the sample calls of the appendix. The exit status is 0 for `allow`, 1 for `deny`, 3 for `escalate`, and 2 for a usage error. A message to a tool server that is a party of an enforced contract is governed whatever its sender, so an actor that is not a party, or not declared at all, is denied by the tool default. Matching is three-valued, and an unknown is resolved fail-closed: an unknown prohibition applies, an unknown permission does not.

The command is stateless: the in-force flags and the valid derived instances come in through the input. `cadl agents-run` (below) keeps that state and calls the same decision function for every message of a recorded trace.

#### Runtime on a Recorded Trace

`cadl agents-run` is the reference runtime of the appendix (G.5 to G.8). It replays a recorded sequence of messages through one logical enforcement point over all enforced contracts of the file and computes the decision for every message, the holds and how they end, the delegation state, and the audit records. It is deterministic and offline: it does not run agents or tools, and its only clock is `t` of the trace.

```bash
cadl agents-run examples/agents_tool_governance.cadl \
    --trace examples/traces/agents_tool_governance_a_approved.jsonl
cadl agents-run FILE --trace TRACE [--format text|json] [--audit OUT.jsonl]
```

For each message, in the order of the trace, the runtime (a) derives the state: the in-force flags of the norms that are not static from the verdict engine of `cadl norms-eval`, the valid derived instances from the grants and revocations seen so far, and the running contract instances with their bindings; (b) calls the decision function of `cadl agents-decide` on the policy input built from that state; (c) applies the decision: an allowed message becomes an observation of the norms engine for the instances of the contracts that govern it, a denied message does not, and an `escalate` creates a hold (`h-1`, `h-2`, ... in the order of creation); (d) appends the audit records. A hold ends when the approver's message creates `approval` or `rejection` through a `counts_as:` rule with the hold id as first argument (`approve("h-1")`), when its timeout passes, or when a contract instance of one of its grounds ends or its contract is amended. An approved message is decided again at the time of the approval and, if nothing denies it, released as the next observation; it does not violate the prohibitions it was held on. In that second decision the approval stands in for the permission and for the grounds only in the contract instances of the grounds: a prohibition that applies anywhere else, a contract instance of a ground that has ended, or another governing contract that no longer allows the message (a revoked delegation, an ended instance, an amendment) denies the release. The file must declare `norms: 0.1` and `agents: 0.1` and pass `cadl check`. The exit status is 0 when the trace was replayed, whatever the decisions are, and 2 on a usage error.

**Trace.** The JSON Lines format of `cadl norms-eval` (`t`, `bindings`, `request`, `values`, `messages`, `state`, `amend`, `end`; see [Norm Verdicts on a Trace](#norm-verdicts-on-a-trace)), read for the whole enforcement point instead of one contract instance, with two more keys:

```json
{"t": 0, "quantities": [], "epoch": "2026-11-10T09:00:00Z", "messages": [{"from": "OPERATOR", "to": "PLANNER", "name": "grant_read"}]}
{"t": 180, "messages": [{"from": "PLANNER", "to": "FILES", "name": "delete_file"}]}
{"t": 220, "messages": [{"from": "OPERATOR", "to": "PLANNER", "name": "approve", "args": ["h-1"]}]}
```

| Key | Meaning |
|---|---|
| `epoch` | First line only: the time of `t = 0` as an ISO 8601 time stamp. With it, the times of the audit records and `now` of the policy input are time stamps; without it they are the numbers of `t` |
| `quantities` | First line only: the quantity names of the run, as a list (`["limit", "target"]`). With it exactly these bare names are quantities from the start; a predicate that reads one before it has a value is undefined. Without it a bare name is a quantity from the first line on that gives it a value and a symbolic value before, so that no decision depends on a later line; the run warns when such a name gets its first value after a message was decided. Giving the key is the recommended form; a contract without bare names in its predicates, like the bundled examples, gives `[]`. A trace should also give the initial `values` on its first line: while a quantity that the `trigger:` of a prohibition or the `until:` of a permission reads has no value, messages are denied or escalated (see Readings) |
| `host_instances` | First line only: the ids of the contracts whose instances a host creates, when the first instance comes on a later line (`["DELIVERY"]`). Whether a contract has host-created instances is fixed on the first line |
| `instances` | Host-created contract instances: a list of entries with `contract`, `instance`, and optionally `bindings`, `request`, `state`, `"end": true`. An entry that names a new instance creates it at this line; a later entry sets its lifecycle `state` or ends it. A contract that the first line neither names here with an instance other than `default` nor lists under `host_instances` has the single instance `default`, from the first line to the end, with the `bindings` and `request` of the first line; a later entry that creates an instance of it is a trace error. Every instance must bind the index variables of the parties of its contract (`"bindings": {"i": 2}` for a party `ROBOT[i]`); otherwise the trace is rejected |
| `t` | A finite number of at most 2**53 in absolute value, so epoch seconds and epoch milliseconds both fit; `NaN`, `Infinity` and larger values are trace errors, also in `cadl norms-eval`. Durations of the file (`timeout:`, `deadline:`) are counted in the unit of seconds. With `epoch`, every time of the trace and every deadline of a hold must be an instant that an ISO 8601 time stamp can express; otherwise the trace is rejected with the line number |
| `messages` | Decided one at a time, in the order written; `from` and `to` are concrete actors (`PLANNER`, `ROBOT[2]`) and `name` is an identifier, written without extra whitespace (`"PLAN NER"` is a trace error) |
| `state` | Applies to every running instance of a contract with a `lifecycle:`; an entry of `instances` sets it for one instance. A terminal state ends the instance |
| `amend` | A new version of the file. Each enforced contract whose definition changed is amended as in `cadl norms-eval`; its held holds are cancelled and its derived instances are tested against the new version (G.7). The set of enforced contracts must stay the same |
| `end` | `true` ends every running instance; so does the last line, unless it says `"end": false` (holds that are still held then stay `held`). Messages on later lines are still decided and recorded: an ended instance does not come back, so a tool call of its contract is denied with `no_instance` |

One line is processed in this order: holds whose deadline lies before `t` expire; instances are created; the time and the `values` of the line (and an `amend`) are one observation without message, so that a deadline or a window that has passed is settled before a message is decided; each message is decided, and an allowed one is one observation; a `state` change and an `end` are a last observation without message.

**Output.** The text report shows the audit log, the holds, the derived instances and the final state of the norms of every contract instance. `--format json` gives one object:

| Key | Content |
|---|---|
| `file`, `trace`, `agents_version`, `contracts` | What was replayed; the enforced contracts |
| `summary` | `messages`, `allow`, `deny`, `escalate` (decisions of the messages), `holds` (count per final state and `held`), `released`, `audit_records` |
| `audit[]` | The audit log (below) |
| `steps[]` | Per message of the trace: `time`, `message` (with `args`), `input` (the policy input the runtime built, as for `agents-decide`), `result`, `hold_id` |
| `releases[]` | Per approved hold: `time`, `hold_id`, `message`, `input` (the policy input at the approval), `grounds` (`contract`, `norm`, `instance`), `released` |
| `holds[]` | `hold_id`, `state` (`held`, `approved`, `rejected`, `expired`, `cancelled`), `message`, `time`, `approver`, `deadline`, `contracts`, `norms`, `closed`, `released` (whether the message took effect) |
| `delegations[]` | Every derived instance: `contract`, `instance`, `norm`, `holder`, `chain`, `state` (`active`, `discharged`), `valid`, `granted`, `ended`, `end_reason` |
| `instances[]` | Per contract instance: `contract`, `instance`, `bindings`, `running`, `start`, `end`, `state`, and from the norms engine `norms[]` (as in `norms-eval`, plus `in_force`), `violations[]`, `institutional_events[]`, `unauthorised[]`, `ok` |
| `amendments[]`, `warnings[]` | Amendments applied (`time`, `file`, `contracts`); warnings |

**Audit log.** `--audit OUT.jsonl` writes the records as JSON Lines, in the order in which the enforcement point writes them; the same file and trace always give the same bytes. A *decision record* is the audit record of G.8: the result of the decision (`decision`, `reason`, `contracts`, `norms`, `provenance`, `escalate_to`, `timeout`, `fallback`, `notify`) with `time`, `message` (`from`, `to`, `name`), `instances` (the running instances of the governing contracts; in a closing record the contract instances of the grounds of the hold, running or not), `hold_id`, and `outcome`. An `escalate` gives two records with the same `hold_id`: the decision, and a closing record whose `outcome` is the final state of the hold and whose reason is `approved`, `rejected`, `expired` (with the deadline as `time`), or `cancelled`, or the reason of the denial when the second decision denies. An *event record* has `event` instead of `decision`, and the fields `time`, `event`, `message`, `instances`, `norm`, `chain`, `holders`, `hold_id`, `reason`:

| `event` | Written when | `reason` |
|---|---|---|
| `grant` | A grant message was observed and conditions 1 to 4 of G.7 hold; `chain` and `holders` give the new derived instance | `null`, or `holder_exists` for the no-op of condition 5 |
| `revoke` | A revoke message was observed; `holders` lists the discharged holders | `null`, or `no_effect` |
| `unauthorised_grant` | A grant message failed one of the conditions 1 to 4 | The first that failed: `no_authority`, `not_in_to`, `in_chain`, `max_depth` |
| `delegation_end` | A derived instance ended for another cause than a revoke message; one record per instance, with its `chain` and holder | `until`, `original_discharged`, `instance_end`, `amendment` |
| `instance_start`, `instance_end` | A contract instance started or ended (`message` is `null`) | `null`; for the end `end` or `terminal_state` |
| `amendment` | A new version took effect; `instances` lists the running instances of the amended contracts | The name of the new version as the trace gives it |
| `unauthorised_approval`, `unauthorised_rejection` | An `approval` or `rejection` changed no hold | `unknown_hold`, `not_held`, `not_approver`, `other_instance` (not created in a contract instance of a ground) |
| `unauthorised_act` | The norms engine recorded an unauthorised act (F.5, item 7) | `no_power` |

```json
{"time": "2026-11-10T09:03:00Z", "message": {"from": "PLANNER", "to": "FILES", "name": "delete_file"}, "instances": [{"contract": "ASSISTED_REPORTING", "instance": "default"}], "decision": "escalate", "reason": "prohibition", "contracts": ["ASSISTED_REPORTING"], "norms": ["ASSISTED_REPORTING.no_delete"], "provenance": [], "escalate_to": "OPERATOR", "timeout": 600000, "fallback": ["PLANNER -> TICKETS : create_ticket"], "notify": ["OPERATOR"], "hold_id": "h-1", "outcome": null}
{"time": "2026-11-10T09:03:40Z", "message": {"from": "PLANNER", "to": "FILES", "name": "delete_file"}, "instances": [{"contract": "ASSISTED_REPORTING", "instance": "default"}], "decision": "allow", "reason": "approved", "contracts": ["ASSISTED_REPORTING"], "norms": ["ASSISTED_REPORTING.no_delete"], "provenance": [], "escalate_to": "OPERATOR", "timeout": null, "fallback": [], "notify": [], "hold_id": "h-1", "outcome": "approved"}
```

The audit file is written only after the whole trace was replayed: when a run fails, no file is written and the error message says so. Changes of the in-force flag of a norm have no record of their own; they follow from the recorded messages, the times, and the `values` of the trace. The log has no integrity protection (no hash chain or signature); G.8 does not ask for one.

**Readings.** Where G.5 to G.8 leave a choice, the runtime reads them as follows.

- A message is an observation only for the instances of the enforced contracts that govern it (G.5); a message that no enforced contract governs is allowed as `ungoverned` and observed by none of them.
- A denied or held message is not an observation: the norms engine never sees it, so it triggers no norm, creates no institutional event, fulfils no obligation, violates no prohibition, and is no grant, revocation, approval or rejection. Time still passes: every line is first observed without its messages.
- The time of a line is observed before its messages are decided, so a prohibition whose window has closed no longer applies, and an obligation whose deadline has passed is already `violated`, when a message of that time is decided.
- Every contract instance binds the index variables of the parties of its contract: the `bindings` of the first line for the instance `default`, the `bindings` of its entry for a host-created instance. An instance without such a binding, or with a value that is not the index of a member of the set the party names, is a trace error that names the contract, the instance, the variable and the value. A binding of a variable that no party has is ignored, with a warning. Together with G-18 this means that no index of an enforced contract is ever unbound in `cadl agents-run`.
- Every decision depends only on the lines up to it: the decisions on a prefix of a trace are the first decisions on the whole trace. For the reading of a bare name in a predicate (`AG.mode == locked`) this means that the runtime does not look ahead as `cadl norms-eval` does: `locked` is a symbolic value until a line gives it a value, unless `quantities` names it. Nothing after the end of an instance changes its verdicts.
- Missing data is unknown, without latching: while the predicate `trigger:` of an inactive prohibition is undefined because a quantity it reads has no value, the prohibition applies; while the predicate `until:` of an active permission is undefined, the permission does not apply. The norm states are those of `cadl norms-eval` (an undefined predicate does not occur); only the in-force flag of the policy input is left out, and it follows the state again as soon as the value is there. The same holds when that `trigger:` or `until:` is an institutional event that a `counts_as:` rule creates from a predicate `when:`, while the `when:` or `if:` of such a rule is undefined. The run warns once per norm and names the quantity. A denial on an unknown flag has the reason `prohibition` like any other; the flag is then absent from `in_force` in `steps[].input`. Not covered: a rule with a message `when:` and an `if:` creates its event at that message or never, so a message that arrives while the `if:` has no value creates nothing (`cadl check` warns about such an event when it triggers a prohibition or ends a permission, G-19, and the run warns when it happens); and an obligation whose predicate `trigger:` has no value is not activated, so a prohibition that is its reparation does not arise. A grant sent while the `until:` of the permission is undefined is `unauthorised_grant` / `no_authority` and has to be repeated.
- A binding has to be the index of a member of every declared set that the variable indexes in the contract (`SUP[i]` in an event beside a party `W[i]`).
- Quantified predicates and aggregates are evaluated as in `cadl norms-eval`. One that is undefined because the domain of a set with a symbolic bound is open (no value for `N`) is missing data: an `exists` trigger of a prohibition makes it apply, a `for all` `until:` of a permission applies it only while a known member falsifies it. A wildcard or range reference outside a quantifier (`W[*].alarm == true`) is rewritten as in `cadl norms-eval` (F.5, "Wildcard and range references") and judged as `for all j in W[*]: W[j].alarm == true`. What stays not evaluable (an aggregate over two sets, `sum(W[*].x * R[*].y)`) makes a norm not judged and its in-force flag unknown for the whole run, so such a prohibition always applies and such a permission never does.
- A trace error on a later line is reported before anything is printed or written; the earlier lines have then been decided in memory only.
- `cadl.agents_runtime.run` and `run_files` refuse a file, or a version it is amended to, that has errors of `cadl check`. `run(..., check=False)` switches that test and the test of the bindings off; it is unsafe and exists for the tests of the next point.
- Defence in depth, reachable only with `check=False`: an occurrence or a predicate that depends on an unbound index is unknown, and an unknown occurrence never has an effect that lets a message through (it does not activate or resume a permission, end, suspend or discharge a prohibition, create an institutional event, exercise a power, or fulfil an obligation) and always has the effects that block (it activates a prohibition and ends a permission or a derived instance).
- The grounds of a hold are kept per contract instance. An `approval` or `rejection` closes a hold only when it was created in a contract instance of a ground; one is enough. The same message creating the event in several instances counts once.
- A closing record with the reason `rejected`, `expired` or `cancelled` keeps the `contracts`, `norms`, `escalate_to`, `fallback` and `notify` of the hold; `timeout` is `null` in every closing record. When the second decision denies an approved message, the record has the result of that denial and the outcome `approved`.
- The second decision of an approved message never creates a second hold: a contract that would escalate denies. Its record lists every contract that denies, with the first of the reasons `no_instance`, `prohibition`, `default`.
- The `until:` of a delegation ends the derived instances that are active when it occurs; a later grant creates a new one.
- A violated obligation is `excused` when a matching message of its bearer was denied by its own contract, or held on a ground of it and not released, between its activation and its deadline (or, without a deadline, the violation). Matching is that of the boundary: sender, receiver and name.
- After a violation, the window clock of a prohibition stops while it is suspended, as before the violation.
- An amendment applies to the enforced contracts whose definition differs between the two versions.

`examples/traces/` has three traces for `examples/agents_tool_governance.cadl`: the three calls of G.12, with a call through a delegation chain, an escalation that is approved, and a denial after a revocation (a); a rejection, the fallback, and a hold cancelled at the end (b); an undeclared tool, a timeout, and a late approval (c). `examples/agents_change_control.cadl` has a prohibition that is in force only between a trigger and an `until:`, and a delegation with `max_depth: 1` and an `until:`; its traces show approval, rejection and expiry during a freeze (a), and the depth limit and the end of a delegation (b).

### Regime Transitions

`cadl regime-map` analyzes the regime transition graph:

- Builds a directed graph of regime states from `transitions:` definitions
- **Reachability analysis** — BFS from initial state to find unreachable regimes
- **Dead-state detection** — identifies states with no outgoing transitions
- **Cycle detection** — Tarjan's SCC algorithm to find cyclic regime patterns
- **Shortest path** — BFS-based shortest path between any two regimes
- **Export formats**: text summary, Graphviz DOT, JSON

### IEC 62853-oriented Summary

`cadl iec62853` summarises a CADL definition around the themes of IEC 62853 (Open Systems Dependability). The indicator names are defined by CADL, not by the standard, and the report does not assess conformance to IEC 62853.

| CADL Concept | CADL-defined indicator |
|---|---|
| alpha (information sharing) | Information Transparency Level |
| beta (authority centralization) | Governance Centralization Index |
| lambda (incentive strength) | Stakeholder Alignment Metric |
| ContractDef | Service Level Agreement (SLA) |
| ViolationBlock | Failure Response Specification |
| TransitionDef | Operational State Machine |
| SoS type (D/A/C/V) | System Integration Level |

### Simulator Config Generation

`cadl sim-*` commands lower a CADL definition to a **3-layer Intermediate Representation (IR)** and generate simulator-specific configuration files:

```
CADL YAML ─── Parser ──► AST ─── Lowering ──► 3-Layer IR ─── Generator ──► Simulator Config
                                                 │
                                    ┌────────────┼────────────┐
                                    ▼            ▼            ▼
                              Layer 1       Layer 2       Layer 3
                            Institution    Protocol     Algorithm
                            /Governance   /Interaction  /Operational
```

| Layer | Research Concern | IR Types | Example |
|---|---|---|---|
| Layer 1: Institution | Who decides? Who knows? Who benefits? | `ActorSpec`, `ContractSpec`, `GovernanceParams` | beta=0.8 → DISPATCHER holds authority |
| Layer 2: Protocol | How do they interact? What on timeout? | `ProtocolSpec`, `StepSpec` | message: ROBOT→DISPATCHER : failure_report |
| Layer 3: Algorithm | What runs centrally vs locally? | `AlgorithmSpec` | central=ECBS, local=tracking_only |

Three simulator targets:

| Target | Format | Key Convention | Use Case |
|---|---|---|---|
| `python` | YAML | snake_case, planner bindings | Python-based MAPF/MAS simulator |
| `unity` | JSON | camelCase, prefab hints | Unity3D visualization |
| `go` | JSON | snake_case, interface spec | Go concurrent simulator |

```bash
# Validate IR
cadl sim-validate examples/a_sos_robot_delivery.cadl

# Print IR (YAML or JSON)
cadl sim-ir examples/a_sos_robot_delivery.cadl
cadl sim-ir examples/c_sos_taxi_fleet.cadl --format json

# Generate simulator configs
cadl sim-gen examples/a_sos_robot_delivery.cadl --target python -o sim_config.yaml
cadl sim-gen examples/a_sos_robot_delivery.cadl --target unity -o sim_config.json
cadl sim-gen examples/c_sos_taxi_fleet.cadl --target go -o sim_config.json
```

#### A-SoS vs C-SoS Comparison

| Property | A-SoS (robot delivery) | C-SoS (taxi fleet) |
|---|---|---|
| SoS Type | Acknowledged | Collaborative |
| Decision Authority | DISPATCHER (central) | TAXI[*] (each taxi) |
| beta (per contract) | 0.8, 0.9 (centralized) | 0.1, 0.05 (decentralized) |
| Central Planner | ECBS | aggregation_only |
| Local Planner | tracking_only | LRA* + conflict avoidance |
| Sharing Mode | uplink + broadcast | peer-to-peer broadcast |

#### Raspimouse Swarm Simulator — D-SoS / C-SoS / MCP-SoS Comparison

Three CADL definitions model the same 5-robot swarm on an 11-node graph network under different SoS paradigms. These connect to the raspimouse-swarm-simulator (a research repository that is not public; the part needed for the hands-on course is published as [cadl-raspimouse-simulator](https://github.com/ertlnagoya/cadl-raspimouse-simulator)) via the Unity config generator (`cadl sim-gen --target unity`).

| Property | D-SoS (Directed) | C-SoS (Collaborative) | MCP-SoS (Acknowledged) |
|---|---|---|---|
| Decision Authority | ARBITRATOR | ROBOT[*] (propose) + ARBITRATOR (verify) | LLM_AGENT |
| beta | 0.9 | 0.3 | 0.6 |
| alpha | 0.2 | 0.7 | 0.9 |
| Central Planner | NaiveDijkstra | DirectionDijkstra | LLM_Dijkstra |
| Local Planner | none | DirectionDijkstra | NaiveDijkstra + OccupancyAware |
| Communication | NATS req/res | NATS req/res + resource query | MCP tools |
| Regimes | NORMAL ↔ CONGESTED | NORMAL ↔ CONGESTED | NORMAL ↔ COLLISION_RESOLUTION ↔ DEADLOCK |

The beta and alpha rows show the values of the main contract of each definition; the C-SoS and MCP-SoS files contain a second contract with its own values.

```bash
# Generate Unity configs for all three modes
cadl sim-gen examples/raspimouse_d_sos.cadl --target unity -o output/raspimouse_d_sos_unity.json
cadl sim-gen examples/raspimouse_c_sos.cadl --target unity -o output/raspimouse_c_sos_unity.json
cadl sim-gen examples/raspimouse_mcp_sos.cadl --target unity -o output/raspimouse_mcp_sos_unity.json
```

### Norm Verdicts on a Trace

`cadl norms-eval` computes the verdicts of the norms extension (Appendix F, F.4 and F.5 of the specification) for one contract instance on a recorded trace. It is an offline monitor: it does not run the system, and it does not run the lifecycle state machine.

```bash
cadl norms-eval examples/norms_robot_delivery.cadl \
    --trace examples/traces/norms_robot_delivery_b_reparation.jsonl
cadl norms-eval FILE --trace TRACE [--contract ID] [--format text|json]
```

The file must declare `norms: 0.1` and pass `cadl check`. `--contract` is needed only when several contracts have `norms:` or `counts_as:`. The exit status is 0 when no norm and no guarantee has the verdict `violated` (`excused` and `pending` do not fail), 1 otherwise, and 2 on a usage error (no `norms: 0.1`, type-check errors, a malformed trace).

**Trace.** JSON Lines, one observation per line, in non-decreasing time. One trace is one contract instance.

```json
{"t": 0.0, "bindings": {"i": 2}, "values": {"DISPATCHER.is_operational": true, "ROBOT[2].status": "Idle"}}
{"t": 1.0, "messages": [{"from": "DISPATCHER", "to": "ROBOT[2]", "name": "route_assignment"}], "state": "Assigned"}
{"t": 2.5, "messages": [{"from": "ROBOT[2]", "to": "DISPATCHER", "name": "ack", "args": ["accepted"]}]}
{"t": 120.0, "values": {"ROBOT[2].status": "Delivered"}, "state": "Completed"}
```

| Key | Meaning |
|---|---|
| `t` | Time in seconds (required); `now` in predicates. A finite number of at most 2**53 in absolute value |
| `bindings` | First line only: the indices the instance binds (`ROBOT[i]` is read as `ROBOT[2]`) |
| `request` | First line only: the fields of `request` |
| `values` | Updates of the snapshot, keyed by the name as written in predicates (`ROBOT[2].battery`, `delivery_time`); a value stays until it changes |
| `messages` | Messages sent at this observation: `from`, `to`, `name`, optional `args`; `from` and `to` are concrete actors (`DISPATCHER`, `ROBOT[2]`) and `name` an identifier, written without extra whitespace. A message event matches on sender, receiver and name; when the CADL text gives arguments (`ack(accepted)`), they must equal `args`. An actor reference matches as F.3 says: `ROBOT[1..2]` only `ROBOT[1]` and `ROBOT[2]`, `ROBOT[*]` and an unbound `ROBOT[i]` only members of the declared set, an undeclared actor never |
| `state` | Lifecycle state of the instance after this observation; it stays until it changes and starts as `lifecycle.initial`. Predicates of an observation read the state before it (step 6 of F.5 comes last) |
| `amend` | Path of a new version of the CADL file, relative to the trace file: the contract is amended at this observation (see below) |
| `end` | `true` ends the instance here. It also ends at the first terminal `state`, and otherwise at the last line, unless that line says `"end": false`: the trace is then a prefix and nothing is settled |

**Output.** The text report has a timeline and tables of norms, violations, institutional events, unauthorised acts, assumptions and guarantees. `--format json` gives the same as one object:

| Key | Content |
|---|---|
| `file`, `trace`, `contract`, `norms_version` | What was evaluated |
| `instance` | `bindings`, `start`, `end` (null for a prefix), `ended`, `end_reason`, `state` (last lifecycle state), `observations` |
| `norms[]` | `id`, `kind`, `bearer`, `state` (the six states of F.5), `verdict`, `activated_at`, `time` and `reason` of the last change, `evaluable`, `history[]` (`t`, `from`, `state`, `reason`) |
| `violations[]` | `t`, `norm`, `severity`, `verdict` (`violated` or `excused`), `sanction`, `reparation`, `reparation_activated`, `transition` (the requested lifecycle state; it is reported, not applied) |
| `institutional_events[]` | `t`, `name`, `performer` (the sender of the message; null for a predicate), `source` |
| `unauthorised[]` | `t`, `act`, `performer`, `reason`: the content of a power performed while no matching power of the performer is active |
| `amendments[]` | `t`, `file`, `effects[]` (`norm`, `effect`, `from_state`, `to_state`, `reason`); `effect` is `carry_over`, `discharge`, `transform`, `activated`, `new_definition`, `removed` or `added` |
| `assumptions[]` | `index`, `text`, `temporal`, `status` (`holds`, `failed`, `pending`), `time`, `reason` |
| `guarantees[]` | `index`, `text`, `temporal`, `verdict`, `time`, `reason` |
| `timeline[]` | `t`, `kind` (`start`, `norm`, `violation`, `event`, `power`, `unauthorised`, `ignored`, `amend`, `assume`, `guarantee`, `end`), `text` |
| `warnings[]`, `violated[]`, `ok` | Warnings; the norms and guarantees with verdict `violated`; whether there are none |

**Readings.** Where F.4 and F.5 leave a choice, the command reads them as follows.

- Every change carries the time of the observation at which it is decided. A missed deadline is detected, and judged `violated` or `excused`, at the first observation after it; the reason gives the time the deadline passed.
- An `assume:` or `guarantee:` item with a temporal operator is evaluated once, at the start of the instance. An item without one is a state predicate and is judged at every observation: it fails at the first observation at which it is false, and is `holds` / `fulfilled` when the instance ends without that.
- A temporal operator is decided false (`within`, `until`) or true (`throughout`) only by an observation after its interval. If the instance ends first, the item stays `pending`, and so does a norm whose temporal content is not decided.
- A predicate that uses a name without a value in the trace is undefined: it does not hold, it does not make an assumption or a guarantee fail, and the name is reported once. A bare name without a value is an enum-like literal only when `==`, `!=` or `IN` compares it with a string value (`status == Delivered`); anywhere else (`ok == true`, `flag` alone) it is undefined. A function call other than a temporal operator or an aggregate of a comprehension is not evaluable: the norm or item stays `pending` with that reason.
- Quantifiers (`exists j in ROBOT: ...`, `for all r in ROBOT[*]: ...`) and the aggregates `sum`, `count`, `min`, `max`, `avg` of a comprehension are evaluated as F.5 ("Quantifiers and aggregates") defines. The domain is a declared set (`A`, `A[*]`, `A[lo..hi]`; for a comprehension also `lo..hi`); the variable is the index of a member (`ROBOT[j]`, `r.load`, `j != k`); a reference such as `ROBOT[j+1]` whose index is not a member is undefined. `count` counts the elements that are true or a nonzero number. With integer bounds the domain is closed and the quantifiers follow Kleene's logic. A symbolic bound (`ROBOT[1..N]`) has a value when the trace binds `N` (`bindings`) or `N` is a quantity with an integer value at that observation; a quantifier variable named `N` does not hide it (and `cadl check` warns, N-14). The domain is closed only when the bound has a value and no member beyond it (`ROBOT[5]` with `N` = 3) has been named so far, which F.3 makes a member (a warning says so), and when it has at most 10 000 members; a larger one is evaluated as an open domain, with a warning, so that a large `N` in a trace cannot make the evaluation slow. Otherwise the domain is open, its known members are those the trace has named so far (value keys such as `ROBOT[3].delivery`, senders and receivers, and a binding `i` where the contract writes `ROBOT[i]`), `exists` is true only by a known witness, `for all` false only by a known counterexample, and everything else, including every aggregate, is undefined (a warning says so).
- Monotone aggregates in a comparison (F.5): `count`, `max` and `min` of a comprehension compared (`<`, `<=`, `>`, `>=`, `==`, `!=`, either side) with an operand that has no quantifier and no comprehension are decided from the known members when every completion agrees. `count` lies between the members where the element is true and that plus the undefined ones (no upper end on an open domain), `max` from the largest defined value up, `min` from the smallest down. So `count(ROBOT[*].delivery == request.id) >= 2` is true as soon as two known robots carry the delivery, also while `N` has no value, and `count(...) <= 1` is false then; `count(...) == 2` is never true over an open domain. On a closed domain with every element defined the value is exact, as before. An element with a value of the wrong kind (a text for `max`, a text or symbolic value for `count`) still makes the aggregate undefined. `sum`, `avg`, an aggregate inside arithmetic, and a comparison of two aggregates are not refined.
- A wildcard or range reference outside a domain is shorthand (F.5, "Wildcard and range references"), rewritten when the contract is read and then evaluated as the quantifier or comprehension it stands for. In the argument of `sum`, `count`, `min`, `max` or `avg` it ranges over the members: `count(ROBOT[*].status == Collision)` is `count(ROBOT[j].status == Collision for j in ROBOT[*])`. Anywhere else the smallest comparison that contains it (or, outside every comparison, the smallest operand of `AND`, `OR`, `NOT`, quantifier body or temporal argument) must hold for every member: `ROBOT[*].battery > 10` is `for all j in ROBOT[*]: ROBOT[j].battery > 10`; `ROBOT[*].x > ROBOT[*].y` compares each member with itself; `ROBOT[*].x == BASE[*].y` must hold for every pair; `NOT ROBOT[*].stopped` is `NOT (for all ...)`, true when some robot has not stopped. `ROBOT[2..N]` ranges over that range, with the open-domain rules above. `cadl check` warns where the reading may surprise (N-15), naming the quantified form that was probably meant: under `NOT`, two sets in one comparison, the same set in both operands of `OR`, the same set on both sides of one comparison (`ROBOT[*].pos != ROBOT[*].pos` compares each robot with itself; also inside an aggregate), a set inside `within`/`throughout`/`until` (all members at one observation), and an aggregate over two sets, which is not evaluated. A file without such references gives the results of 0.3.23. `cadl.norms_eval.desugar_wildcards(expr)` returns the rewritten predicate.
- Trigger-less norms become active before step 1 of the first observation. Within step 4 the order is deadline, content, `until:`. Content while a norm is suspended does not count. A violation found when the instance ends is reported, but activates no reparation and requests no transition. A norm activated at the observation at which the instance ends is `discharged`.

**Amendments.** A line with `"amend": "new_version.cadl"` replaces the contract by the contract with the same id in that file, before the other steps of the observation, and treats the norm instances as F.6 says. An active or suspended instance follows the `on_amendment:` of the old version: `carry_over` keeps its state and elapsed time under the new definition (it is `discharged` when the id is gone or the kind differs, and an obligation is `violated` at once when the elapsed time exceeds the new deadline), `discharge` discharges it, and `transform: X` discharges it and activates an inactive `X` with the elapsed time counted. An inactive instance takes the new definition or is removed, a new norm without trigger is active at once, and final instances are not affected. The `counts_as:` rules and the `lifecycle:` of the new version apply from then on. The new file must declare `norms: 0.1`, pass `cadl check`, have the contract, and have the current lifecycle state; otherwise the exit status is 2. Where F.6 is silent: an `assume:` / `guarantee:` item whose text is unchanged continues, a new item is evaluated from the amendment on, an undecided item that is gone stays `pending`, and a carried-over prohibition whose elapsed time exceeds a new window is `fulfilled`.

`cadl norms-diff OLD NEW [--contract ID] [--format text|json]` reports, without a trace, what such an amendment does: norms removed, added or of a changed kind, every norm that carries over with a changed `content` or `deadline:` (the report F.6 asks for), `transform:` targets the new version lacks, and changed `counts_as:` rules. Each finding has a level (`error`, `warning`, `info`); JSON has `old`, `new`, `contracts[]` (`id`, `findings[]` with `level`, `kind`, `text` and details), `errors[]` and `ok`. The exit status is 0 when nothing prevents the amendment, 1 on an error (a missing transform target, an invalid new version), and 2 on a usage error.

`examples/traces/` has five traces for `examples/norms_robot_delivery.cadl`: all norms fulfilled (a), a missed acknowledgement repaired by a failure report (b), both missed (c), a late delivery excused because the dispatcher stopped operating (d), and a reassignment that discharges the obligations (e). `norms_amendment_v1_to_v2.jsonl` is a trace of `examples/norms_amendment_v1.cadl` that is amended to `examples/norms_amendment_v2.cadl` while a failure report is due.

`examples/norms_no_double_assignment.cadl` adds to that contract a prohibition quantified over the robots: the same delivery must not be assigned to two robots at once (`exists j in ROBOT: exists k in ROBOT: j != k AND ROBOT[j].delivery == request.id AND ROBOT[k].delivery == request.id`). Its traces: no double assignment (a, fulfilled), `ROBOT[3]` also gets the delivery at t=3 (b, violated at t=3), and a reassignment to the same robot (c, fulfilled). `N` has no value in them, so the domain is open: the content is true once two named robots carry the delivery, and undefined, which does not violate, while no two do.
### Norms Monitor in Generated Python Code

For a file that declares `norms: 0.1`, `cadl codegen -t python` also writes `norms_monitor.py`: an online monitor of one contract instance (Appendix F, F.5). A running system feeds it observations as they happen and gets back the changes of the norm states and of the four-valued verdicts, including those of `assume:` / `guarantee:` items with temporal operators, which `contracts.py` leaves out.

```python
from robot_delivery_norms.norms_monitor import NormsMonitor   # the generated package

m = NormsMonitor("DELIVERY_SLA", bindings={"i": 2})
m.observe({"t": 0.0, "values": {"DISPATCHER.is_operational": True, "ROBOT[2].status": "Idle"}})
m.observe({"t": 1.0, "state": "Assigned",
           "messages": [{"from": "DISPATCHER", "to": "ROBOT[2]", "name": "route_assignment"}]})
# -> [{"t": 1.0, "kind": "norm", "id": "acknowledge", "state": "active", "verdict": "pending",
#      "previous_state": "inactive", "previous_verdict": "pending", "reason": "trigger occurred: ..."}]
m.next_deadline()        # 6.0: acknowledge is due 5 s after its activation
m.next_check()           # 6.000000002: the first time stamp at which that is detected
m.observe({"t": 6.5})    # -> acknowledge violated, report_failure active
m.verdicts()             # current state and verdict of every norm and item
m.end()                  # the instance has ended: settle the norms
m.result()               # everything, with the keys of `cadl norms-eval --format json`
```

| Call | Meaning |
|---|---|
| `NormsMonitor(contract=None, bindings=None, request=None, quantities=None, versions=None)` | A monitor of one contract instance. `contract` may be left out when one contract has `norms:` or `counts_as:` |
| `observe(observation)` | Process one observation: a dict with the keys of one trace line of `cadl norms-eval` (`t` required; `values`, `messages`, `state`, `end`; `bindings` and `request` in the first one). Returns the changes of this step |
| `end()` | End the instance at the last observation received and settle its norms (F.5, item 6). An observation with `"end": True` or with a terminal `state` ends it too |
| `verdicts()` | `norms` (`state`, `verdict` per id), `assumptions` (`status`), `guarantees` (`verdict`), `ok` |
| `result()` | The result of `cadl norms-eval --format json` for the observations so far, without `file`; before the end it is the result of a prefix |
| `next_deadline()` | The earliest time at which a deadline of an active obligation or the window of an active prohibition is over, or `None`. An observation at exactly this time is still in time |
| `next_check()` | The earliest time stamp at which an observation is certain to decide something that only waits for time: such a deadline, or a temporal operator whose interval is over (also nested ones, and those in the content of a norm). `None` when nothing waits |
| `started`, `ended`, `contract` | State of the monitor |

A change is a dict: for a norm `t`, `kind: "norm"`, `id`, `state`, `verdict`, `previous_state`, `previous_verdict`, `reason` (one per norm and step, with the state the step left it in); for an item `t`, `kind: "guarantee"` or `"assumption"`, `index`, `text`, `verdict` (or `status`: `holds`, `failed`), `previous`, `reason`.

**One implementation.** `norms_monitor.py` does not import cadl; it needs only the standard library of Python 3.9 or later and can be copied on its own. It holds the declared actors and the contracts of the file as data and, copied verbatim, the engine of `cadl norms-eval` (the part of `cadl/norms_eval.py` between its `monitor-runtime` marks) with the AST classes and helpers it uses. Where the engine calls the parser of cadl, the monitor looks up what the parser returned at generation time. Before the module is written, the generator compiles it and checks that every global name it uses is defined in it; if the copied code needs something that was not copied, `cadl codegen` fails with that message and writes nothing. The generated package can be imported where cadl is not installed; its other modules still need `cadl.codegen.runtime_support`, and their names raise the import error when used. A recorded trace is replayed with

```bash
python -m robot_delivery_norms.norms_monitor TRACE.jsonl [--contract ID] [--format json|text]
```

which prints what `cadl norms-eval` prints for the same file and trace (JSON without the key `file`), with the same exit status. The tests compare the two on the example traces, on every case of `tests/test_norms_eval.py`, and on 1,500 random traces (with actor ranges, members outside a range and undeclared actors), in a process in which cadl cannot be imported.

**What an online monitor knows.** The monitor decides each observation with the observations received so far, so `result()` after an observation is the result of `cadl norms-eval` on the trace up to it as a prefix (`"end": false`), given the same names of quantities (third point). Three points differ from reading a whole trace:

- A missed deadline, and a temporal operator that is false (`within`, `until`) or true (`throughout`) because its interval is over, is detected at the first observation after that time, and a violation is judged `violated` or `excused` then. A host with nothing to report sends a time-only observation `{"t": m.next_check()}` when that time has come. `next_check()` is a little later than the deadline or the end of the interval: times are compared with a tolerance of 1e-9 s, content at the deadline itself is in time, and at large times the step of the floating-point numbers is larger than the tolerance. A norm without deadline whose content has a temporal operator is evaluated at every observation, so every observation gives it a new time to check.
- The instance does not end "at the last line": call `end()`, or send an observation with `"end": True` or a terminal `state`. An observation after the end has no effect: it is checked for its form, counted in a warning of `result()`, and nothing of it is kept.
- A name written without an actor (`load`, not `ROBOT[1].load`) that has no value yet cannot be told from a symbolic value such as `Delivered`. `cadl norms-eval` takes the names of the quantities from the observations of the instance in the trace; the monitor takes them from `quantities=[...]`, and a name that is not given there is a quantity only from its first value on. This is the one way in which the verdicts can differ, and only for such a name compared with a text value before its first value (`mode == target`, where `target` gets its value later): give the names with `quantities=`. Warnings about names without a value are given when they are first met, and a `state` for a contract without `lifecycle:` is reported when it arrives, also when a later observation amends the contract (`cadl norms-eval` then says nothing).

A refused observation leaves the monitor exactly as it was, and the monitor stays usable. `observe` raises `TraceError` for an observation that is malformed (a wrong type, a time that goes back, is not finite or is larger than 2**53, an actor that is not written as `NAME` or `NAME[2]`, a state the lifecycle does not have) and `ObservationError` (a `MonitorError`) for an amendment that cannot be applied. The monitor copies what it is given. It keeps the observations of its instance (temporal operators and "becomes true" need them): make one monitor per contract instance and drop it after the end. It is not thread-safe, and, like `cadl norms-eval`, it reads the lifecycle state from the observations and reports a requested `transition:` without applying it.

**Amendments.** An observation with `"amend": "NAME"` needs the model of the new version: generate the monitor of the new file too and pass its `MODEL`, `NormsMonitor(..., versions={"NAME": new_package.norms_monitor.MODEL})`; on the command line `--model-version NAME=MODEL.json`, where the file is written by `python -m NEW_PACKAGE.norms_monitor --dump-model`. A value of `versions` that is not such a model is refused by the constructor. An observation that names a version the monitor does not have, a version without the contract, or one whose lifecycle lacks the current state is refused (`ObservationError`). Loading a `.cadl` file at run time is not possible, since the monitor has no parser. At an observation with `amend`, a norm that is new in the new version is reported with `previous_state` `None` and a removed one with `state` `None`; the `index` of an item is its position in the current version, so identify an item across an amendment by its `text`.

**Strings and numbers of the file.** Everything the monitor takes from the `.cadl` file is in `MODEL`: one JSON text (ASCII, no NaN or Infinity) in one Python string literal, read with `json.loads`; none of it is formatted into code. A model with a number that is not finite is not generated (`cadl codegen` reports where it is and writes nothing); a predicate with a number that overflows, such as `1e999`, is not a predicate for `cadl` at all and is kept as text. The input check of `cadl codegen` still applies to the whole file, so a string with a quote, a backslash or a control character is refused before anything is generated.

The Solidity and OPA targets do not monitor norms; they leave the temporal items out and say so in a warning.

### Grounding of the Institutional Parameters (grounding 0.1)

The grounding extension (Appendix J of the specification) relates the declared `alpha`, `beta` and `lambda` of a contract to its norms. A file declares `grounding: 0.1` together with `norms: 0.1`, which it requires, and may then use four keys:

```yaml
contracts:
  - id: DELIVERY_SLA
    parties: [DISPATCHER, "ROBOT[*]", "CUSTOMER[*]"]
    authority:   { decision_holder: DISPATCHER, beta: 0.9 }
    information:
      alpha: 0.6
      sharing: ["DISPATCHER -> ROBOT[*] : route"]
    incentives:  { lambda: 0.5 }
    grounding:
      items: { route: DISPATCHER, battery: "ROBOT[*]" }   # information item: its origin
      tolerance: 0.25                                      # default 0.25
    norms:
      - id: may_return_to_charge
        kind: permission
        bearer: "ROBOT[i]"
        content: "ROBOT[i] -> DISPATCHER : charging_request"
        shares: [battery]                                  # the message conveys these items
      - id: no_low_battery_driving
        kind: prohibition
        bearer: "ROBOT[i]"
        content: "ROBOT[i].battery < 10 AND ROBOT[i].status == InTransit"
        on_violation: { sanction: "removed from the pool", magnitude: 4 }
        on_fulfilment: { reward: "bonus", magnitude: 1 }
```

`cadl norms-indices FILE [--contract ID] [--format text|json]` computes three quantities for every contract, each an exact ratio of two counts, or undefined when its denominator is zero (J.4):

| Quantity | Ratio | Reported with it |
|---|---|---|
| **alpha^**, information sharing | pairs (item, party other than its origin) that a sharing entry, an obligation or a permission gives to the whole party / all such pairs | the pairs given in each of the three ways, the pairs not given, and the entries and norms whose target is member-specific (`ROBOT[3]`), which give no pair |
| **beta^**, concentration of powers | largest number of powers borne by one single actor / powers | the powers per bearer, the number of norms each power names in `effects:`, and the share of the `decision_holder` |
| **chi**, incentive coverage | obligations and prohibitions with a sanction text, a reward text or a magnitude greater than zero / obligations and prohibitions | the norms with a reparation only, and the norms with a `transition:` |

```console
$ cadl norms-indices examples/grounding_robot_delivery.cadl
Contract DELIVERY_SLA (tolerance 0.25)
  alpha^ (information sharing) = 5/8 = 0.625  given pairs / pairs
    declared alpha: 0.6, declared - index = -0.025
    pairs given by a sharing entry: 3, by an obligation: 1, by a permission: 1
    ...
  beta^ (concentration of powers) = 1/2 = 0.5  largest number of powers of one single actor / powers
    declared beta: 0.9, declared - index = 0.4
    ...
  chi (incentive coverage) = 1/3 = 0.333333  norms that carry an incentive / obligations and prohibitions
    declared lambda: 0.5 (a scale; not compared with chi)
    ...
  warning  contract 'DELIVERY_SLA' declares beta: 0.9, but the index of concentration of powers is 1/2 = 0.5 ...; they differ by 0.4, more than the tolerance 0.25 (J-7)
```

JSON has `file`, `grounding`, `contracts[]` (`id`, `items`, `tolerance`, and `alpha`, `beta`, `coverage`, each with `defined`, `numerator`, `denominator`, `exact`, `value` or `undefined` with the reason, and the counts above; `diagnostics[]`) and `designs[]` (the diagnostics of J-9).

**The SoS.** For a file with two or more contracts (and no `--contract`), a last section `SoS: all N contracts` and the JSON key `sos` give the same three quantities over all contracts (J.4, "The quantities of the SoS"): **alpha^_SoS** = the given pairs of all contracts / their pairs, an item being qualified by its contract (an item declared in two contracts gives pairs in each, and an entry of one contract gives no pair of another); **beta^_SoS** = the largest number of powers that one single actor bears in all contracts together / all powers; **chi_SoS** = the obligations and prohibitions with an incentive / all of them. `sos` has `contracts`, and `alpha`, `beta`, `coverage` in the form above with the summed counts (`bearers` and `single_actors` sum the powers per bearer; `largest_bearers` names every single actor with the largest count, sorted; the whole section does not depend on the order of the contracts). Nothing compares them with a declared value (v0.3 declares α, β, λ per contract only), and they are not weighted. alpha^_SoS and chi_SoS lie between the values of the contracts; beta^_SoS is at most the mean of the contracts' beta^ weighted by their powers and can be smaller than all of them (two contracts whose only powers are borne by X and by Y: 1 and 1, SoS 1/2). The IR carries them as `institution.grounding` (J.10). The output for each contract is unchanged. The exit status is 0 when the indices were reported, whatever the warnings, and 2 on a usage error (no file, a type-check error, or a file that does not declare `grounding: 0.1` with `norms: 0.1`).

The indices are evidence about the norms, not the meaning of the parameters: `cadl check` compares the declared `alpha` and `beta` with them and only warns (item 15 under Type Checks). `lambda` is a scale and is not compared with a number. Declaring `grounding: 0.1` never makes a file invalid that declares `norms: 0.1` and uses none of the new keys.

**Annotations only.** `shares:`, `on_fulfilment:` and the magnitudes change no verdict: `cadl norms-eval`, `cadl agents-run` and the generated `norms_monitor.py` give the same results with and without them, and the generated monitor does not carry them. The code generators do not write them either, so a reward text may contain a quote. The IR (`cadl sim-ir`) carries them for a file that declares the extension: each contract gains `grounding` (`items`, `tolerance`, and `indices` with the counts of `alpha`, `beta` and `coverage`, `null` for an undefined quantity) and each norm `shares`, `on_violation_magnitude`, `on_fulfilment_reward` and `on_fulfilment_magnitude` (J.10); with two or more contracts the institution layer gains `grounding` with the quantities of the SoS. The IR of a file that does not declare the extension is unchanged.

**Two versions.** `cadl norms-diff OLD NEW` also reports a changed `on_violation.magnitude`, `on_fulfilment:`, `shares:`, `grounding.items` and `grounding.tolerance` and each changed index, lists the magnitude, `on_fulfilment:` and `shares:` of a norm added in the new version, and warns when a declared `alpha` or `beta` and its index move in opposite directions between the versions (J-13); when only one version declares the extension, the indices are not compared and the report says so, for example when `beta` is raised while the amendment gives a power to the robots.

**For strategic models.** In a strategic model (Appendix I; see Strategic Model below), a payoff term may name a norm instead of giving a weight: `{ reward: shortcuts, sanction_of: "DELIVERY.no_shortcut" }`. Its weight is then −m · ℓ (`reward_of:`: +m′ · ℓ), with m the `on_violation.magnitude` (m′ the `on_fulfilment.magnitude`) of the norm and ℓ the parameter bound by `from: "C.lambda"` (J.7). `cadl.grounding.norm_payoff_link(sos, "C.n", "sanction_of" | "reward_of")` resolves the reference: the magnitude (exact), the bearer, the source name `C.lambda`, or a description of why the reference cannot be used. See `examples/games_grounding_robot_delivery.cadl`.

### Stochastic Model (PRISM Export)

A file that declares `stochastic: 0.1` under `extensions:` may carry a `model:` block (Appendix H of the specification): a parametric discrete-time Markov chain written as guarded probabilistic commands over finite state variables, in which parameters can be bound to `alpha`, `beta`, or `lambda` of a contract. Each regime of `transitions:` is a *design* with its own parameter values and commands. See `examples/stochastic_robot_delivery.cadl`.

```bash
# One PRISM model file and one properties file per design:
#   <sos name>_<design>.prism and <sos name>_<design>.props
cadl model-export examples/stochastic_robot_delivery.cadl --output prism_out/
cadl model-export examples/stochastic_robot_delivery.cadl --output prism_out/ --regime CENTRALIZED

# Evaluate model.properties for every design at the fixed parameter values
cadl model-check examples/stochastic_robot_delivery.cadl
cadl model-check examples/stochastic_robot_delivery.cadl --regime CENTRALIZED --set p_fail=0.2 --set p_loss=1/6 --format json

# Evaluate a regime map query at the corner points of a uniform grid
cadl model-check examples/stochastic_robot_delivery.cadl --regime-map best_for_delivery --grid 5

# Compute the certified regime map of the query (cells decided by parameter lifting)
cadl model-check examples/stochastic_robot_delivery.cadl --regime-map best_for_delivery --certify
cadl model-check examples/stochastic_robot_delivery.cadl --regime-map best_for_delivery --certify --max-cells 500 --format json
```

- `cadl model-export` follows the export rules of H.11. Parameters listed in the `over:` of a regime map are written as undefined constants (`const double p;`), so the files can be given to PRISM or Storm for model checking, region verification, and parameter synthesis. A name equal to a PRISM keyword or built-in function name (`floor`, `ceil`, `round`, `pow`, `mod`, `log`) gets a trailing underscore; a name collision after that is an error.
- `cadl` runs no external model checker. Without `--certify`, `cadl model-check` is a small **internal reference evaluator** for checking the export and for tests: it builds the Markov chain of one design by explicit enumeration, with every parameter fixed, and evaluates `P` with `F`, `F<=k`, `U`, `U<=k` and `R{"r"}` with `F` in exact rational arithmetic. It reports the value and, for a threshold, `satisfied` or `violated`. The exit status is 0 when every threshold property evaluated is satisfied (queries do not count), 1 when at least one is violated, and 2 when nothing could be evaluated (a usage error, or a file without an interpreted model).
- `--regime-map ID --grid N` prints, for each sample point, the objective of each compared regime and which regime is best there. These are **estimates at sample points, not a certified regime map**: no cell is decided, and the exit status is 0.
- `--regime-map ID --certify` computes the **certified regime map** of H.9: a set of cells (boxes of the open parameters) that cover the region, each `decided` with the set `best` of regimes that are optimal at every valuation of the cell, `infeasible` (no regime satisfies the constraint anywhere in the cell), or `undecided`. The method is **parameter lifting** with exact rational arithmetic, inside `cadl` and without an external tool: on a cell, each design is relaxed to a Markov decision process in which every state chooses a corner of the cell for the parameters in its outgoing probabilities; the minimal and maximal values of that process are lower and upper bounds of the design's value on the whole cell. A cell is `decided` for a regime when the bounds prove that it satisfies the constraint everywhere in the cell and that every other regime either violates the constraint everywhere or is nowhere better; otherwise the cell is bisected, the largest undecided cell first, until the undecided fraction of the volume is at most `resolution:`. `--max-depth N` (default 24) bounds the bisections of one cell and `--max-cells N` (default 20000) the size of the partition; when a budget stops the refinement first, the map is still printed and says that the resolution is not reached. The output lists the method, the counts, the undecided fraction (exact and rounded) and every cell with its box, status and `best`; halves with the same claim are joined. The exit status is 0 when the resolution is reached, 1 when it is not, and 2 on an error. `--certify` cannot be combined with `--grid` or `--regime`; `--set` may fix a parameter that is not open in the map. For the example the map has 95 cells (60 decided, 35 undecided, undecided fraction 51/1024) and is computed in about 0.1 s.
- Limits of the certified map: the bounds of the designs are compared with each other, not the difference of their values, so cells next to a boundary stay `undecided` although no boundary crosses them, and regimes that tie are listed together only when the bounds prove it (equal constant bounds, or designs with the same model). A probability that is not multi-affine in the open parameters (`p * p`) is enclosed by interval arithmetic, which is coarser and decides well-definedness on a cell only where the intervals stay within `[0, 1]`. Cells that are not graph-preserving are analysed, not declined; there an expected reward has a finite upper bound only when every choice of corners reaches the target with probability 1. The cost grows with the number of open parameters, cells and states (exact linear solves, at most 2 000 unknowns).
- Limits of this implementation: rules S-7 and S-9 are decided by enumerating the full state space, and a model with more than 200 000 states is refused; the evaluator refuses a linear system with more than 2 000 unknowns and a grid with more than 10 000 points.
- `cadl model-export` leaves out a transition reward whose action label no command of the design has (PRISM and Storm reject such an item) and writes `true : (0);` in a reward structure that would otherwise be empty.

### Strategic Model (Equilibrium Checks and Maps)

A file that declares `stochastic: 0.1` and `games: 0.1` may give its `model:` block `kind: smg` (Appendix I of the specification): a turn-based stochastic game on the model of Appendix H. `players:` declares the players, `owner:` on a command says who chooses it, `profiles:` names profiles of memoryless deterministic strategies as ordered rules `{guard, choose}`, `compliance:` names the profile the contracts ask for, `payoffs:` gives every player a signed weighted sum of expected rewards accumulated until a target, and `equilibria:` asks whether a profile is a Nash equilibrium (or an ε-equilibrium) against unilateral deviations. See `examples/games_robot_delivery.cadl` (a robot that may skip the handling procedure, sanctioned in proportion to λ) and `examples/games_two_players.cadl` (the dispatcher also chooses whether to monitor).

```bash
# Equilibrium checks at the fixed parameter values (entries of model.equilibria without over:)
cadl game-check examples/games_robot_delivery.cadl
cadl game-check examples/games_robot_delivery.cadl --equilibrium comply_nash --regime CENTRALIZED --set lambda=0.2 --format json

# Equilibrium maps (entries with over:): cells certified as equilibrium / not_equilibrium, or undecided
cadl game-check examples/games_robot_delivery.cadl --certify
cadl game-check examples/games_robot_delivery.cadl --certify --equilibrium comply_by_lambda --max-cells 500 --format json

# Start from a uniform partition and report the cells as analysed (the 16 cells of I.12)
cadl game-check examples/games_two_players.cadl --certify --equilibrium watched_map --partition 4 --no-merge

# Properties and regime maps of a strategic model are evaluated under a profile (default: compliance)
cadl model-check examples/games_robot_delivery.cadl
# The game in the model language of PRISM-games (model file only)
cadl model-export examples/games_robot_delivery.cadl --output prism_out/
```

- An **equilibrium check** reports, per design and player, the payoff under the profile, the best-response value, the deviation gain, and the canonical best response (the profile's command wherever it is optimal, otherwise the first optimal command in declaration order), as exact rational numbers; then the `status` (`equilibrium` or `not_equilibrium` with the `deviators`), and `from_every_state` (whether no player gains from any state it can reach; only with `equilibrium` and ε = 0). The best response is computed by policy iteration with exact linear solves on the Markov decision process of one player against the others' fixed strategies; because a profile must be *admissible* (no single player can keep the game away from the target of its payoff), a memoryless deterministic best response is optimal among all strategies, also those with memory and randomisation (I.7).
- An **equilibrium map** covers the region of `over:` with cells. `equilibrium` and `not_equilibrium` are claims about **every** valuation of a cell and are certified: on a cell, the game of each player against the profile is relaxed (every state chooses, per allowed command, a distribution at a corner of the cell; an exact interval enclosure for a probability that is not multi-affine), and the cell is decided by the sufficient conditions N, E1 and E2 of I.9 from the exact optimal values of the relaxed processes. A cell is decided only when every policy of every player's relaxed process reaches the target with probability 1 and the design is shown to be well-defined on the cell; anything else is `undecided`, with a `reason` (`bounds`, `not_admissible`, `not_well_defined`). The largest undecided cell is bisected until the undecided fraction is at most `resolution:` or a budget (`--max-depth`, default 24; `--max-cells`, default 20 000) is exhausted; cells that carry the same claim are then joined (`--no-merge` keeps them as analysed). No floating-point number is used.
- Exit status of `cadl game-check` (I.13): **0** when every requested result was computed, whatever the answers; **1** when a map did not reach its resolution or a query was not evaluated; **2** when nothing can be claimed (a static error, a usage error, a design that is not well-defined or a profile that is not admissible at the values used). `--set` rechecks these conditions at the values it uses and cannot set a parameter that an evaluated map opens. Without `--certify` the maps are not computed, and the output says so.
- `cadl model-check` on a strategic model evaluates each property, and each regime map, in the Markov chain of its `profile:` (default: the compliance profile). `cadl model-export` writes one model file per design with `smg` and one `player … endplayer` construct per player that owns a command of the design, and no properties file (I.11). **These files have not been run through PRISM-games.** `cadl sim-ir` carries the game in `stochastic_model` (I.13).
- **The link to norms (Appendix J, J.7).** In a file that also declares `norms: 0.1` and `grounding: 0.1`, a payoff term `sanction_of: "C.n"` has the weight −m · ℓ and a term `reward_of: "C.n"` the weight m′ · ℓ, with m (m′) the `on_violation.magnitude` (`on_fulfilment.magnitude`) of the norm and ℓ the parameter bound by `from: "C.lambda"`; `cadl sim-ir` writes the defined weight (`"-2 * lambda"`) with the fields `sanction_of` and `reward_of`. Every result computed with such a term carries the assumption of J.7 ("the reward structure r is taken to count the violations of C.n"), in the text and in the JSON (`assumptions`). `cadl check` reports, per design, the expected count of each `sanction_of:` and `reward_of:` term under the compliance profile at the fixed values (J-12): for a `sanction_of:` term, 0 means that compliance incurs no sanction of the term, which is condition (ii) of J.7; a `reward_of:` term breaks condition (ii) by the sign of its weight whatever its count. Without `grounding: 0.1` such a term is an error (I-7); a term whose weight is not defined is never read with a default weight, and its queries report `not_evaluated` (exit status 1).
- **End-point certification (J-14).** In a file that declares `grounding: 0.1`, for an equilibrium map with one open parameter ℓ (whether or not it is bound to `C.lambda`) that occurs in no probability and no reward value and with a power of at most one in every weight, counted on the text (the powers of the factors of a product add up, no cancellation), a cell [a, b] that the conditions of I.9 leave undecided is `equilibrium` when the profile is an ε-equilibrium at a and at b (exact evaluations; the gains are convex in ℓ), and a player p is a deviator on it when its gain at b exceeds ε and condition (ii) of J.7 holds for p: at the values used for the other parameters, every weight of p in which ℓ occurs is −m·ℓ with m ≥ 0, and its reward structure has the expected value 0 under the profile (the gain is then non-increasing). Such cells carry `method: "endpoints (J-14)"`; the map reports whether J-14 applies and, if not, why (`j14`; in a file that does not declare `grounding: 0.1` the reason is that J-14 is a rule of that extension). No other claim follows from the end points.
- Limits. Strategies named in a file are memoryless and deterministic; there are no mixed equilibria, no concurrent moves, and no partial observation (I.1). The conditions of a map are sufficient, not necessary: cells next to the boundary of the equilibrium region stay `undecided` however fine the partition; a cell on which the relaxed process of some player has a policy that never reaches the target (for example a cell that contains a valuation at which a probability becomes 0 and a cycle closes) stays `undecided` although the profile is admissible on it; the deviation tried for `not_equilibrium` is the canonical best response at the centre of the cell. A cell lists the deviators that are established for the whole cell, so a coarse cell may list fewer than its parts would. I-3 forbids a player id that is a keyword of Appendix H, but the examples of I.12 name a player `R`; this processor admits `F`, `U`, `P`, and `R` as player ids (they are renamed in the export, as H.11 says).

### AI Integration

`cadl ai` generates CADL from natural language descriptions via Claude API:

1. User provides an NL description (Japanese or English)
2. Claude generates a CADL definition using few-shot examples
3. The generated CADL is parsed and type-checked
4. With `--verify`, it also goes through the SMT and deadlock checks of `cadl verify`; a check that fails counts as a validation error
5. On validation errors, the system retries with error feedback

Requires `ANTHROPIC_API_KEY` environment variable and `pip install "cadl-lang[ai]"`.

### Type Checks

The type checker (`cadl check`) validates:

1. **Actor reference existence** — Actors referenced in parties, authority, information sharing, protocol steps, and `assume:` / `guarantee:` predicates are defined. In a predicate a name is treated as an actor when it is indexed (`ROBOT[i]`) or is the object of a member access (`ROBOT.battery`); a bare name is taken as a state variable
2. **Contract party consistency** — Every contract has parties, and no party is listed twice
3. **Protocol step validity** — Senders and receivers match actor definitions
4. **Information sharing coherence** — Sharing declarations reference valid actors
5. **Parameter range constraints** — `0 <= alpha, beta, lambda <= 1`
6. **Unique ids and known severities** — No duplicate actor, contract, protocol, or metric ids; `severity:` is `Minor`, `Major`, or `Critical`
7. **Identifiers** — Actor, contract, protocol, and metric ids and regime names are ASCII identifiers and are not reserved words (`AND`, `OR`, `NOT`, `IN`, `in`, `exists`, `true`, `false`)
8. **Sharing entries** — Each `sharing:` entry is a quoted string `"SOURCE -> TARGET : item"`; an unquoted or malformed entry is an error, and an item with arguments is accepted with a warning
9. **Autonomy** — `autonomy:` is `low`, `medium`, or `high`
10. **SoS-DSL static semantics** (Appendix E, E.4) — in a `lifecycle:`, the initial, terminal, `from`, `to`, and `on_violation.transition` states are listed in `states`, and a terminal state exists and is reachable (L-1, L-2, L-3, L-5); the `on_match.transition` of a monitor is a state of the same contract (M-3); states, transition ids, and monitor ids are identifiers and the ids are unique; the actors of a message event in `on:` are declared, and so is the object of each `observe:` entry (M-1). A `deadline:` is a duration (an integer with `ms`, `s`, `min`, or `h`) and a `sampling:` is `event` or `periodic(<duration>)`; a value that cannot be read is an error. As lenient forms, a non-negative number of seconds (`2.5`), a space before the unit (`5 s`, `periodic( 500ms )`), and the mapping `{kind: periodic, period_ms: <integer>}` are accepted. Warnings: a `deadline:` without `on_violation:` (L-4) and an `on:`, `when:`, or `rule:` that is not a rule of Appendix E
11. **Norms static semantics** (Appendix F, F.7; only when the file declares `norms: 0.1`) — norm ids are identifiers, unique in the contract, and differ from the institutional events named by `creates:` (N-1); `kind:` is `obligation`, `permission`, `prohibition`, or `power` (N-2); `bearer:` and `counterparty:` are declared actors without a wildcard or range (N-3); `deadline:` is a duration, and `deadline:` and `on_violation:` belong to obligations and prohibitions (N-4); `effects:` belong to powers, which need at least one, and name other norms of the contract (N-5); a `reparation:` is an obligation or prohibition without `trigger:`, and reparations form no cycle (N-6); `on_violation.transition` is a state of the contract's `lifecycle:` (N-7); the actors of a message event are declared, and a power is exercised by an institutional event created from a message or by a message its bearer sends (N-9); `within(d, p)`, `throughout(d, p)`, and `until(p, q, d)` have a duration literal `d > 0` and appear only in `assume:`, `guarantee:`, and a norm's `content:` (N-10); `on_amendment: {transform: X}` names an identifier (N-11). Warnings: a norm id used as a whole `trigger:` / `until:` / `content:` (N-8), a permission and a prohibition with the same bearer and content (N-12), a prohibition whose message trigger is also an occurrence of its message content, which violates it when it activates it, also through a counts_as event (N-13, best effort), a quantifier or comprehension variable named like a symbolic bound (N-14), a wildcard or range reference whose reading may surprise: under `NOT`, two sets in one comparison, one set in both operands of `OR`, an aggregate over two sets (N-15), a bearer or counterparty that is not a party (N-3), and an obligation or prohibition whose message is not sent by its bearer (N-9). Without the declaration, `norms:` and `counts_as:` get one warning as undeclared extension keys and are neither checked nor lowered to the IR
12. **Stochastic model static semantics** (Appendix H, H.10; only when the file declares `stochastic: 0.1`) — `model:` has `variables:` and `commands:`, and `kind:` is `dtmc` (S-1); ids, label names, enumeration literals, and action labels are identifiers, are no keywords, and are declared once in their namespace, a label is not named `init` or `deadlock`, and the exported names `x_L` of enumeration literals are distinct from every declared name and from each other (S-2); `from:` names `alpha`, `beta`, or `lambda` of a declared contract or a numeric entry of `context.environment` (S-3); `range:` is `[lo, hi]`, required except for a parameter bound to a contract, and every value lies in it (S-4); a variable is `bool`, `"[lo..hi]"`, or `"{A, B}"` with an `init:` of its domain (S-5); every state predicate conforms to the grammar and is well typed (S-6); an update assigns declared variables values of their type that stay in the domain wherever the guard is true (S-7); probabilities are numbers, expressions over parameters, or `rest`, at most one `rest` per command, and they sum to exactly 1 (S-8); at most one command is enabled in any state of any design (S-9); `model.regimes:` and `when:` name regimes of the file, and need `transitions:` (S-10); every parameter has a fixed value under every design, where probabilities lie in `[0, 1]` and rewards are not negative (S-11); a reward structure has items whose `action:` labels a command and whose `value:` mentions no parameter opened by a regime map (S-12); a property conforms to the property grammar, with `R` only over `F` without a step bound and thresholds in range (S-13); a regime map has one to four open parameters, at least two regimes, one objective that is a query, a threshold constraint, a resolution in `(0, 1)`, and designs that are well-defined at the corner points of its region (S-14). S-7 and S-9 enumerate the state space (at most 200 000 states). Warnings: two parameters with the same source (S-3) and a regime without an entry in `model.regimes:` (S-10). Informational: the keys and values reserved for a later version (S-15). Without the declaration, `model:` gets one warning as an undeclared extension key and is not checked, lowered to the IR, or exported

13. **Agent governance static semantics** (Appendix G, G.9; only when the file declares `agents: 0.1`) — the file also declares `norms: 0.1` (G-1); `agent:` has a `kind:` of `llm_agent`, `tool_server`, `human`, or `service` and a `protocol:` that is a name (G-2); `tools:` belongs to a tool server, which lists at least one, and tool names are unique identifiers (G-3); a message event whose receiver is a tool server names one of its tools (G-4; a warning for a protocol step); `enforcement.default` is `deny` or `allow` and `enforcement.point` is `caller`, `receiver`, or `gateway` (G-5); in an enforced contract the sender of the message content of a permission or prohibition is its bearer (G-6); `on_denied:` belongs to a prohibition with a message content in an enforced contract, has at least one key, and its `timeout:` is a duration greater than zero that requires `escalate_to:` (G-7); `escalate_to:` and `notify:` name single declared actors, and `escalate_to:` names a party other than the bearer (G-8); a contract with `escalate_to: A` has a `counts_as:` rule that creates `approval` from a message of `A`, and `approval` and `rejection` are reserved in an enforced contract (G-9); a `fallback:` is a message event of the bearer to a declared actor that differs from the prohibited message (G-10); `delegations:` belongs to an enforced contract, and each entry names one permission with a message content whose bearer has no bound index (G-11), lists declared actors other than tool servers under `to:`, and has a `max_depth:` of at least 1 (G-12); `grant:` and `revoke:` are identifiers that differ from each other, from `approval` and `rejection`, and from the tools of the parties (G-13); in an enforced contract the sender and the receiver of every message event (`trigger:`, `until:` and `content:` of a norm, `when:` of a `counts_as:` rule, `until:` of a delegation) are covered by one reference in the parties, because the contract observes only the messages it governs, and name only declared members (G-17), and every index variable of the contract (in bearers, counterparties, `escalate_to:`, `notify:`, `fallback:`, the senders, receivers and arguments of its message events, and its predicates, also inside an index expression such as `ROBOT[j+1]`) is bound: by a party (`i` for a party `ROBOT[i]`), by a quantifier, or by a comprehension (G-18). Warnings: an institutional event created under an `if:` that is the trigger of a prohibition or the `until:` of a permission (G-19), `escalate_to:` without `timeout:` (G-7), an approver that is not `human` (G-8), no rule for `rejection` (G-9), the bearer listed under `to:` (G-12), a permission or prohibition whose content is not a message event or has arguments (G-14), tools left open by `default: allow` and a tool server that is a party of no enforced contract (G-15), and a message the contract needs but would itself deny (G-16). Without the declaration, `agent:`, `enforcement:`, `delegations:`, and `on_denied:` get one warning as undeclared extension keys and are neither checked nor lowered to the IR

14. **Strategic model static semantics** (Appendix I, I.10; only when the file declares `games: 0.1`) — the file also declares `stochastic: 0.1` (I-1); `kind:` is `dtmc` or `smg`, a model with `kind: smg` has `players:`, `profiles:`, `compliance:`, and `payoffs:`, and any other model has none of the keys of the extension (I-2); player ids are unique identifiers and every player lists declared actors, none of which stands behind two players (I-3); `owner:` names a player, an owned command has an action label, and the commands with one label have one owner or none (I-4); in every state of every design the enabled commands are at most one, or all belong to one player and have different labels (I-5, which replaces S-9); profile names are unique identifiers, a rule chooses the label of an owned command under a guard without labels, every profile selects a command wherever two or more are enabled, and `compliance:` names a profile (I-6); every player has exactly one payoff, with a boolean target, at least one term, declared reward structures used at most once, and weights over declared parameters; a term with `sanction_of:` or `reward_of:` needs `grounding: 0.1` (I-7); every profile is admissible in every design at the fixed values (I-8); an entry of `equilibria:` has an id that is unique among properties and regime maps, a profile, designs, an `epsilon:` that is not negative, and, for a map, one to four open parameters within their ranges that no regime assigns and no reward value mentions, a `resolution:` in `(0, 1)`, designs that are well-defined at the corners of the region, and a profile that is admissible at the centre of every face of the region (I-9); `profile:` of a property or regime map names a profile (I-10). I-5 and I-6 are decided by enumerating the state space, I-8 and I-9 on the reachable game. Warnings: a profile rule that is the first applicable rule in no state (I-6); a payoff whose target holds in the initial state (I-7). Informational: reserved keys and values (I-11); the keys of the extension in a file that does not declare `games: 0.1`, which is then read by Appendix H (I.15). A file that declares `games: 0.1` without `stochastic: 0.1` gets the error of I-1

15. **Grounding static semantics** (Appendix J, J.8; only when the file declares `grounding: 0.1`) — the file also declares `norms: 0.1` (J-1); the keys of `grounding.items` are identifiers, not reserved words, and their values actor references to a party of the contract, and `tolerance:` is a number in `[0, 1]` (J-2); `shares:` belongs to an obligation or a permission whose content is a message event and lists at least one declared item, each once (J-3); a magnitude is a finite number that is not negative, and `on_fulfilment:` belongs to an obligation or a prohibition and has a `reward:` (a text) or a `magnitude:` (J-4). Warnings, never errors: a sharing norm that gives an item to its origin, a target that is not a party or is the bare name of an actor set, a sharing entry with an undeclared item, and a sharing entry with more than one item or with arguments, which gives no pair (J-3); a magnitude of zero next to a sanction or reward text (J-4); `alpha: 0` with a given pair or, when the index is undefined, with a sharing entry, `alpha: 1` with a pair that is not given, `beta: 1` with a power that the single actor with the most powers does not bear, `beta: 0` with a single actor that bears more than an even share (J-6); a declared `alpha` or `beta` other than 0 and 1 farther from its index than the tolerance, and a decision holder with fewer powers than another actor (J-7); for a contract with norms, `lambda` greater than zero while no obligation or prohibition carries a sanction or a reward, and `lambda: 0` while one does (J-8). Informational, in a file that also declares `stochastic: 0.1`: a design in which a parameter bound to `C.alpha` or `C.beta` is farther from the index than the tolerance, unless J-6 or J-7 already reports the same declared value (J-9). The comparisons are exact (decimals as written, ratios as fractions). Without the declaration, `grounding:`, `shares:`, `on_fulfilment:` and `on_violation.magnitude` get one warning as undeclared extension keys, are not interpreted, and no index is computed (J.12). In a strategic model (Appendix I): a term has at most one of `weight:`, `sanction_of:`, `reward_of:`; the reference names an obligation or a prohibition of a declared contract with a magnitude greater than zero for the key; the model has exactly one parameter bound to `C.lambda`; a player that stands for the bearer of the norm owns the payoff (an actor reference that covers the bearer); and a norm is named at most once per key (J-10, errors). Warnings: a norm of a linked contract with a magnitude, a bearer that a player stands for, and no term that names it; a written weight with the parameter bound to `C.lambda` (J-11). Informational: the expected count of each `sanction_of:` and `reward_of:` term under the compliance profile, per design (J-12). J-14 is used by `cadl game-check --certify`

It also reports, without failing the check, a `codegen:` target that `cadl codegen` cannot emit (warning), an extension name it does not know, and a `motivation:` block, which it keeps and passes on in `cadl sim-ir` but does not interpret (informational).

## Hands-on

A self-contained textbook, followed in about 95 minutes on your own or as a 5-session class, walks you through the complete CADL toolchain end-to-end on a robot delivery System of Systems: **CADL modelling → SoS-DSL contracts (lifecycle + monitors) → visualisation → code generation → live simulation**.

The hands-on materials include:

- **Textbook (Course A)** — eleven chapters and four appendices in English and Japanese: the SoS background and the essentials of CADL, then six 15-minute procedures using a runnable end-to-end script (`scripts/sos_dsl_handson_e2e.sh`). Chapters 4–9 end with graded ★ / ★★ / ★★★ exercises for a 5-session class (a compact 3-session plan and a rubric are in Appendix C).
- **Further courses** — Course B (urban mobility with SUMO), Course D (a town that amends its own rules) and Course C (your own SoS).
- **Academic background** — Maier's five SoS criteria, the **ISO/IEC/IEEE 21839 / 21840 / 21841** standards, related research (ADLs, Normative MAS, Runtime Verification), and an annotated bibliography.

| Audience | Entry point |
| --- | --- |
| Self-learner — quick tour | [Textbook — Course A: Robot Delivery](https://www.ertl.jp/cadl-spec/docs/textbook/) (EN) / [JA](https://www.ertl.jp/cadl-spec/ja/docs/textbook/) |
| Student in a class | [Textbook, Appendix C — Using this textbook in a class](https://www.ertl.jp/cadl-spec/docs/textbook/appendix-c-teaching/) (EN) / [JA](https://www.ertl.jp/cadl-spec/ja/docs/textbook/appendix-c-teaching/) |
| Researcher needing citations | [Why SoS-DSL? (academic background)](https://www.ertl.jp/cadl-spec/docs/handson/academic-background) (EN) / [JA](https://www.ertl.jp/cadl-spec/ja/docs/handson/academic-background) |

The materials are hosted in the [cadl-spec repository](https://github.com/ertlnagoya/cadl-spec) — English sources under `docs/handson/`, Japanese under `i18n/ja/docusaurus-plugin-content-docs/current/handson/` — and rendered on the [Hands-on section of the spec website](https://www.ertl.jp/cadl-spec/docs/handson/).

To run the full pipeline locally on the bundled robot-delivery example:

```bash
./scripts/sos_dsl_handson_e2e.sh
```

This generates the IR JSON, the Unity C# tree, and drops the latter into the Unity project of [cadl-raspimouse-simulator](https://github.com/ertlnagoya/cadl-raspimouse-simulator) when it is cloned next to this repository (otherwise pass `--unity ""` to stop after code generation). See the hands-on textbook for the rest of the walkthrough.

## Examples

### CADL Definitions

| File | Description | SoS Type | Key Features |
|---|---|---|---|
| `robot_delivery.cadl` | Autonomous delivery fleet | Acknowledged | Route coordination, failure replanning, 2 regimes |
| `smart_city_traffic.cadl` | Traffic signal management | Collaborative | 3 regimes (NORMAL/CONGESTED/EMERGENCY), emergency override |
| `supply_chain.cadl` | Manufacturing supply chain | Collaborative | 4-actor chain, quality recall protocol, 4 regimes |
| `iot_data_sharing.cadl` | IoT sensor aggregation | Virtual | Data freshness contracts, privacy policy, anomaly detection |
| `household_chores.cadl` | Family chore sharing | Collaborative | Human-centric, monetary incentives, dispute resolution |
| `a_sos_robot_delivery.cadl` | MAPF robot delivery (A-SoS) | Acknowledged | Central ECBS planner, 3 regimes, fleet safety |
| `c_sos_taxi_fleet.cadl` | Autonomous taxi fleet (C-SoS) | Collaborative | Decentralized LRA*, peer conflict resolution, 3 regimes |
| `raspimouse_d_sos.cadl` | Raspimouse swarm (D-SoS) | Directed | Centralized arbitration via NATS, NaiveDijkstra, beta=0.9 |
| `raspimouse_c_sos.cadl` | Raspimouse swarm (C-SoS) | Collaborative | Local DirectionDijkstra + central verification, discrete-time sync |
| `raspimouse_mcp_sos.cadl` | Raspimouse swarm (MCP-SoS) | Acknowledged | LLM-controlled via MCP tools, Static/Dynamic path modes, 3 regimes |
| `sos_dsl_robot_delivery.cadl` | Robot delivery with the SoS-DSL extension | Acknowledged | Contract lifecycle (7 states), 3 monitors; the example used by the hands-on course |
| `agents_tool_governance.cadl` | LLM planner, sub-agent and two tool servers (agents 0.1) | — | Enforced contract, escalation to a human, delegation with provenance; sample inputs in `agents_inputs/`, traces for `cadl agents-run` in `traces/` |
| `grounding_robot_delivery.cadl` | Robot delivery with norms and the grounding extension (grounding 0.1) | Acknowledged | The example of Appendix J, J.9: information items, `shares:`, a sanction magnitude; indices 5/8, 1/2 and coverage 1/3 with `cadl norms-indices` |
| `agents_change_control.cadl` | Owner, lead agent, two workers and a repository (agents 0.1) | — | A prohibition in force between a trigger and an `until:`, delegation with `max_depth: 1` and `until:`; traces for `cadl agents-run` in `traces/` |

### Demo Scripts

Run the demo scripts to see the full toolchain in action:

```bash
# End-to-end workflow: parse -> verify -> codegen (Python/Solidity/Rego) -> regime map -> IEC 62853
python examples/demo_robot_delivery.py

# Regime transitions, dependability summary, and multi-target generation
python examples/demo_smart_city.py

# Side-by-side comparison of Python, Solidity, and Rego output
python examples/demo_codegen_targets.py

# 3-layer simulator IR and A-SoS / C-SoS comparison
python examples/demo_sim_ir.py
```

## Project Structure

```
src/cadl/
  __init__.py          Package root
  ast_nodes.py         AST node definitions (30+ dataclasses)
  grammar.lark         Lark grammar for the expression sub-language
  parser.py            Hybrid parser (YAML structure + Lark expressions)
  type_checker.py      Static semantic checks
  verifier.py          SMT-based contract verification (Z3)
  deadlock.py          Protocol deadlock detection
  regime_map.py        Regime transition graph analysis
  iec62853.py          IEC 62853-oriented dependability summary
  unparse.py           Expression AST back to CADL source text
  norms_eval.py        Verdicts of the norms extension on a recorded trace
  agents_runtime.py    Runtime of the agent governance extension: decisions, holds, delegations and audit records on a trace
  norms_diff.py        Effect of an amendment on the norms (two versions compared)
  grounding.py         Grounding extension: derived indices, rules J-1 to J-9 and J-13
  cli.py               Command-line interface
  codegen/
    __init__.py        Public generate() API (multi-target dispatch)
    runtime_support.py Base classes for generated code
    expr_compiler.py   Expression AST -> Python source
    emitter.py         Code emission utilities
    actor_gen.py       Actor class generation
    contract_gen.py    Contract monitor generation
    protocol_gen.py    Protocol state machine generation
    transition_gen.py  Regime controller generation
    metric_gen.py      Metrics collector generation
    runtime_gen.py     Orchestrator generation
    norms_monitor_gen.py       Norms monitor generation (norms_monitor.py of the Python target)
    norms_monitor_template.py  Text copied into the generated norms monitor (not imported)
    solidity/
      solidity_expr.py Expression AST -> Solidity source
      solidity_gen.py  Solidity smart contract generation
    opa/
      rego_expr.py     Expression AST -> Rego source
      rego_gen.py      OPA/Rego policy generation
    agent_policy/
      rego_gen.py      Boundary policy as Rego (agents 0.1)
      cedar_gen.py     Boundary policy as Cedar, with the side table (agents 0.1)
    unity_csharp/
      contract_emitter.py  Per-contract C# (state enum, contract, monitors)
      runtime_template.py  C# runtime support emitted with every generation
    safety.py          Input checks applied before code generation
  ai/
    __init__.py        Public generate_cadl() API
    prompts.py         System prompts and few-shot examples
    llm_client.py      Claude API client wrapper
    nl_to_cadl.py      NL-to-CADL generation pipeline
  sim/
    __init__.py        Public API: lower_to_ir, validate_ir, generate_config
    ir.py              3-layer IR dataclasses (SimIR, InstitutionLayer, etc.)
    lower.py           AST -> IR lowering
    validate.py        IR-level validation
    gen_python.py      IR -> Python simulator YAML config
    gen_unity.py       IR -> Unity JSON config
    gen_go.py          IR -> Go JSON config

tests/
  test_parser.py         Parser tests
  test_type_checker.py   Type checker tests
  test_verifier.py       SMT verifier tests
  test_deadlock.py       Deadlock detector tests
  test_codegen.py        Python code generation tests
  test_ai.py             AI integration tests
  test_runtime.py        Runtime base class tests
  test_regime_map.py     Regime map graph analysis tests
  test_solidity_gen.py   Solidity code generation tests
  test_opa_gen.py        OPA/Rego code generation tests
  test_iec62853.py       IEC 62853-oriented summary tests
  test_sim_ir.py         Simulator IR lowering and validation tests
  test_sim_gen.py        Simulator config generator tests
  test_sim_ir_sos_dsl.py SoS-DSL lifecycle / monitors in the simulator IR
  test_sos_dsl_extension.py       SoS-DSL parser tests
  test_unity_csharp_codegen.py    Unity C# generation tests
  test_unity_csharp_structural.py Structural checks on the generated C#
  test_expr_parser.py    Expression grammar tests
  test_unparse.py        Expression round-trip tests
  test_norms_eval.py     Norm verdicts on a trace (`cadl norms-eval`)
  test_grounding_extension.py  Grounding extension: indices, rules, IR, `cadl norms-indices`
  test_grounding_sos.py  Grounding extension: the quantities of the SoS over all contracts
  test_norms_wildcards.py Wildcard and range references in predicates (F.5): rewriting, equivalence with the quantified forms, N-15
  test_norms_monitor.py  Generated norms monitor against `cadl norms-eval` (examples, recorded and random traces)
  norms_monitor_cases.py pytest plugin that records the cases of test_norms_eval.py for it
  test_name_resolution.py Actor vs state-variable resolution in generated code
  test_method_dispatch.py Verification method dispatch tests
  test_codegen_safety.py Code generation input-safety tests
  test_examples_verify.py Every example passes `cadl verify`
  test_actor_reference_check.py   Which names `cadl check` treats as actors
  test_conformance_checks.py      Identifier rules, sharing entries, codegen targets, extensions
  test_codegen_deterministic.py   Generated code is identical across runs
  test_keyword_boundaries.py      Keywords are not split out of longer names
  test_agents_extension.py        Agent governance extension: G-1 to G-19, decisions, agent-rego / agent-cedar
  test_agents_runtime.py          Runtime of the agent governance extension (`cadl agents-run`): holds, delegations, audit records

examples/
  robot_delivery.cadl        Robot delivery SoS (Acknowledged type)
  smart_city_traffic.cadl    Smart city traffic management (Collaborative type)
  supply_chain.cadl          Supply chain management (Collaborative type)
  iot_data_sharing.cadl      IoT data sharing (Virtual type)
  household_chores.cadl      Family chore sharing (Collaborative type)
  demo_robot_delivery.py     End-to-end workflow demo
  demo_smart_city.py         Regime map & IEC 62853 demo
  demo_codegen_targets.py    Multi-target code generation demo
  a_sos_robot_delivery.cadl  A-SoS MAPF robot delivery sample
  c_sos_taxi_fleet.cadl      C-SoS autonomous taxi fleet sample
  raspimouse_d_sos.cadl      Raspimouse swarm D-SoS (Directed)
  raspimouse_c_sos.cadl      Raspimouse swarm C-SoS (Collaborative)
  raspimouse_mcp_sos.cadl    Raspimouse swarm MCP-SoS (LLM-controlled)
  test_raspimouse.sh         Parse, validate, and generate Unity configs for Raspimouse
  sos_dsl_robot_delivery.cadl  Robot delivery with SoS-DSL lifecycle and monitors
  demo_sim_ir.py             3-layer simulator IR and A-SoS / C-SoS comparison demo

scripts/
  sos_dsl_handson_e2e.sh     Hands-on pipeline: check -> Sim-IR -> Unity C# -> Unity project
```

## Status

CADL is alpha software (see [CHANGELOG.md](CHANGELOG.md)); the language and the generated output may change between releases.

| Feature | Status |
|---|---|
| Core language, parser, type checker | Available |
| Verification (SMT-based consistency, deadlock detection) | Available |
| Python runtime code generation | Available |
| Natural language to CADL via Claude API | Available |
| Regime transitions and regime map | Available |
| IEC 62853 report, Solidity and OPA/Rego generation | Available |
| Simulator IR and config generation (Python, Unity, Go) | Available |
| SoS-DSL extension (`lifecycle:` / `monitors:`) and Unity C# generation | Available |
| Norms extension: static checks, and verdicts and amendments on a recorded trace (`cadl norms-eval`, `cadl norms-diff`) | Available |
| Norms extension: online monitor in the generated Python package (`norms_monitor.py`) | Available (Solidity, OPA and Unity C# do not monitor norms; no translation to a model checker) |
| Grounding extension (grounding 0.1), norms side: derived indices and incentive coverage (`cadl norms-indices`), rules J-1 to J-9, IR, comparison of two versions (J-13 in `cadl norms-diff`) | Available |
| Strategic model extension (games 0.1): equilibrium checks and certified equilibrium maps (`cadl game-check`), with the link to norms of Appendix J (`sanction_of:` / `reward_of:`, J-10 to J-12, J-14) | Available (the export for PRISM-games has not been run through PRISM-games) |
| Agent governance extension (agents 0.1): checks, decision function, runtime on a recorded trace (holds, delegations, audit records; `cadl agents-run`), Rego / Cedar boundary policies | Available (the runtime is an offline reference: it replays a trace and is not connected to a protocol) |
| `model_check` / `simulation` / `proof` verification methods | Not supported yet (reported explicitly) |

## Related Projects

- [cadl-spec](https://github.com/ertlnagoya/cadl-spec) — The specification and the hands-on course, published at <https://www.ertl.jp/cadl-spec/>.
- [cadl-explorer](https://github.com/ertlnagoya/cadl-explorer) — A Streamlit application that draws the contract lifecycle from the IR that `cadl sim-ir` produces and explores governance settings on a synthetic model.
- [cadl-raspimouse-simulator](https://github.com/ertlnagoya/cadl-raspimouse-simulator) — The simulator used by the hands-on course: Unity project, Go arbitrator and Python reference runtime for the C-SoS robot-delivery scenario.
- raspimouse-swarm-simulator (research repository, not public; public export: [cadl-raspimouse-simulator](https://github.com/ertlnagoya/cadl-raspimouse-simulator)) — Multi-agent swarm robotics simulation platform. CADL files in `examples/raspimouse_*.cadl` describe its three SoS modes, and the Unity config generator produces configuration JSON for the simulator.

## References

- Benveniste et al., "Contracts for System Design," Foundations and Trends in EDA, 2018.
- ISO/IEC/IEEE 21841:2019, Taxonomy of Systems of Systems.
- Saoud et al., "Assume-guarantee contracts for continuous-time systems," Automatica, 2021.
- IEC 62853:2018, Open Systems Dependability.
- C. Shimoyama and Y. Matsubara, "Governance as a Structural Design Variable: An Empirical Study of Performance-Autonomy Value Spaces in Systems of Systems," in Proc. 21st International Conference on System of Systems Engineering (SoSE), 2026.

## License

[Apache License 2.0](LICENSE). Releases up to and including 0.3.2 were published under the MIT License.
