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
# and generate the boundary policy as Rego or Cedar
cadl agents-decide examples/agents_tool_governance.cadl --input examples/agents_inputs/delete_file.json
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

# What an amendment from one version to the next does to the norms
cadl norms-diff examples/norms_amendment_v1.cadl examples/norms_amendment_v2.cadl

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

Generated code inherits from base classes in `cadl.codegen.runtime_support`.

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

The command is stateless. Holds and their release or expiry (G.6), grants, chains and revocations (G.7), and audit records (G.8) are not implemented: a host runtime keeps that state and supplies the in-force flags and the valid derived instances through the input.

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
| `t` | Time in seconds (required); `now` in predicates |
| `bindings` | First line only: the indices the instance binds (`ROBOT[i]` is read as `ROBOT[2]`) |
| `request` | First line only: the fields of `request` |
| `values` | Updates of the snapshot, keyed by the name as written in predicates (`ROBOT[2].battery`, `delivery_time`); a value stays until it changes |
| `messages` | Messages sent at this observation: `from`, `to`, `name`, optional `args`. A message event matches on sender, receiver and name; when the CADL text gives arguments (`ack(accepted)`), they must equal `args` |
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
- A predicate that uses a name without a value in the trace is undefined: it does not hold, it does not make an assumption or a guarantee fail, and the name is reported once. A bare name without a value is an enum-like literal only when `==`, `!=` or `IN` compares it with a string value (`status == Delivered`); anywhere else (`ok == true`, `flag` alone) it is undefined. A function call other than a temporal operator is not evaluable: the norm or item stays `pending` with that reason.
- Trigger-less norms become active before step 1 of the first observation. Within step 4 the order is deadline, content, `until:`. Content while a norm is suspended does not count. A violation found when the instance ends is reported, but activates no reparation and requests no transition. A norm activated at the observation at which the instance ends is `discharged`.

**Amendments.** A line with `"amend": "new_version.cadl"` replaces the contract by the contract with the same id in that file, before the other steps of the observation, and treats the norm instances as F.6 says. An active or suspended instance follows the `on_amendment:` of the old version: `carry_over` keeps its state and elapsed time under the new definition (it is `discharged` when the id is gone or the kind differs, and an obligation is `violated` at once when the elapsed time exceeds the new deadline), `discharge` discharges it, and `transform: X` discharges it and activates an inactive `X` with the elapsed time counted. An inactive instance takes the new definition or is removed, a new norm without trigger is active at once, and final instances are not affected. The `counts_as:` rules and the `lifecycle:` of the new version apply from then on. The new file must declare `norms: 0.1`, pass `cadl check`, have the contract, and have the current lifecycle state; otherwise the exit status is 2. Where F.6 is silent: an `assume:` / `guarantee:` item whose text is unchanged continues, a new item is evaluated from the amendment on, an undecided item that is gone stays `pending`, and a carried-over prohibition whose elapsed time exceeds a new window is `fulfilled`.

`cadl norms-diff OLD NEW [--contract ID] [--format text|json]` reports, without a trace, what such an amendment does: norms removed, added or of a changed kind, every norm that carries over with a changed `content` or `deadline:` (the report F.6 asks for), `transform:` targets the new version lacks, and changed `counts_as:` rules. Each finding has a level (`error`, `warning`, `info`); JSON has `old`, `new`, `contracts[]` (`id`, `findings[]` with `level`, `kind`, `text` and details), `errors[]` and `ok`. The exit status is 0 when nothing prevents the amendment, 1 on an error (a missing transform target, an invalid new version), and 2 on a usage error.

`examples/traces/` has five traces for `examples/norms_robot_delivery.cadl`: all norms fulfilled (a), a missed acknowledgement repaired by a failure report (b), both missed (c), a late delivery excused because the dispatcher stopped operating (d), and a reassignment that discharges the obligations (e). `norms_amendment_v1_to_v2.jsonl` is a trace of `examples/norms_amendment_v1.cadl` that is amended to `examples/norms_amendment_v2.cadl` while a failure report is due.

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
11. **Norms static semantics** (Appendix F, F.7; only when the file declares `norms: 0.1`) — norm ids are identifiers, unique in the contract, and differ from the institutional events named by `creates:` (N-1); `kind:` is `obligation`, `permission`, `prohibition`, or `power` (N-2); `bearer:` and `counterparty:` are declared actors without a wildcard or range (N-3); `deadline:` is a duration, and `deadline:` and `on_violation:` belong to obligations and prohibitions (N-4); `effects:` belong to powers, which need at least one, and name other norms of the contract (N-5); a `reparation:` is an obligation or prohibition without `trigger:`, and reparations form no cycle (N-6); `on_violation.transition` is a state of the contract's `lifecycle:` (N-7); the actors of a message event are declared, and a power is exercised by an institutional event created from a message or by a message its bearer sends (N-9); `within(d, p)`, `throughout(d, p)`, and `until(p, q, d)` have a duration literal `d > 0` and appear only in `assume:`, `guarantee:`, and a norm's `content:` (N-10); `on_amendment: {transform: X}` names an identifier (N-11). Warnings: a norm id used as a whole `trigger:` / `until:` / `content:` (N-8), a permission and a prohibition with the same bearer and content (N-12), a bearer or counterparty that is not a party (N-3), and an obligation or prohibition whose message is not sent by its bearer (N-9). Without the declaration, `norms:` and `counts_as:` get one warning as undeclared extension keys and are neither checked nor lowered to the IR

12. **Agent governance static semantics** (Appendix G, G.9; only when the file declares `agents: 0.1`) — the file also declares `norms: 0.1` (G-1); `agent:` has a `kind:` of `llm_agent`, `tool_server`, `human`, or `service` and a `protocol:` that is a name (G-2); `tools:` belongs to a tool server, which lists at least one, and tool names are unique identifiers (G-3); a message event whose receiver is a tool server names one of its tools (G-4; a warning for a protocol step); `enforcement.default` is `deny` or `allow` and `enforcement.point` is `caller`, `receiver`, or `gateway` (G-5); in an enforced contract the sender of the message content of a permission or prohibition is its bearer (G-6); `on_denied:` belongs to a prohibition with a message content in an enforced contract, has at least one key, and its `timeout:` is a duration greater than zero that requires `escalate_to:` (G-7); `escalate_to:` and `notify:` name single declared actors, and `escalate_to:` names a party other than the bearer (G-8); a contract with `escalate_to: A` has a `counts_as:` rule that creates `approval` from a message of `A`, and `approval` and `rejection` are reserved in an enforced contract (G-9); a `fallback:` is a message event of the bearer to a declared actor that differs from the prohibited message (G-10); `delegations:` belongs to an enforced contract, and each entry names one permission with a message content whose bearer has no bound index (G-11), lists declared actors other than tool servers under `to:`, and has a `max_depth:` of at least 1 (G-12); `grant:` and `revoke:` are identifiers that differ from each other, from `approval` and `rejection`, and from the tools of the parties (G-13). Warnings: `escalate_to:` without `timeout:` (G-7), an approver that is not `human` (G-8), no rule for `rejection` (G-9), the bearer listed under `to:` (G-12), a permission or prohibition whose content is not a message event or has arguments (G-14), tools left open by `default: allow` and a tool server that is a party of no enforced contract (G-15), and a message the contract needs but would itself deny (G-16). Without the declaration, `agent:`, `enforcement:`, `delegations:`, and `on_denied:` get one warning as undeclared extension keys and are neither checked nor lowered to the IR

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
| `agents_tool_governance.cadl` | LLM planner, sub-agent and two tool servers (agents 0.1) | — | Enforced contract, escalation to a human, delegation with provenance; sample inputs in `agents_inputs/` |

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
  norms_diff.py        Effect of an amendment on the norms (two versions compared)
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
  test_name_resolution.py Actor vs state-variable resolution in generated code
  test_method_dispatch.py Verification method dispatch tests
  test_codegen_safety.py Code generation input-safety tests
  test_examples_verify.py Every example passes `cadl verify`
  test_actor_reference_check.py   Which names `cadl check` treats as actors
  test_conformance_checks.py      Identifier rules, sharing entries, codegen targets, extensions
  test_codegen_deterministic.py   Generated code is identical across runs
  test_keyword_boundaries.py      Keywords are not split out of longer names
  test_agents_extension.py        Agent governance extension: G-1 to G-16, decisions, agent-rego / agent-cedar

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
| Agent governance extension (agents 0.1): checks, decision function, Rego / Cedar boundary policies | Available (the stateful runtime of holds, delegations and audit is not implemented) |
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
