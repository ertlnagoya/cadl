# CADL: Contract Architecture Description Language

English | [日本語](README_ja.md)

CADL is a domain-specific language for formally describing, verifying, and deploying institutional designs in Systems of Systems (SoS). It enables stakeholders — from engineers to citizens — to define governance rules, contracts, protocols, and incentive structures in a machine-readable, verifiable format.

CADL is developed at ERTL, Graduate School of Informatics, Nagoya University.

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

# Analyze regime transitions
cadl regime-map examples/smart_city_traffic.cadl
cadl regime-map examples/smart_city_traffic.cadl --format dot -o regime.dot

# IEC 62853 compliance report
cadl iec62853 examples/robot_delivery.cadl
cadl iec62853 examples/smart_city_traffic.cadl --format json

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
   [Verified AST]
         |
    +----+----+----------+-----------+
    v         v          v           v
  SMT       Code       Regime      IEC 62853
  Verify    Generate   Map         Compliance
  (Z3)      (codegen/) (regime_map) (iec62853)
              |
        +-----+-----+
        v     v     v
     Python Solidity OPA/Rego
```

### Code Generation

`cadl codegen` generates executable code from a verified CADL definition. Three targets are supported:

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

### Regime Transitions

`cadl regime-map` analyzes the regime transition graph:

- Builds a directed graph of regime states from `transitions:` definitions
- **Reachability analysis** — BFS from initial state to find unreachable regimes
- **Dead-state detection** — identifies states with no outgoing transitions
- **Cycle detection** — Tarjan's SCC algorithm to find cyclic regime patterns
- **Shortest path** — BFS-based shortest path between any two regimes
- **Export formats**: text summary, Graphviz DOT, JSON

### IEC 62853 Compliance

`cadl iec62853` maps CADL constructs to IEC 62853 Open Systems Dependability concepts:

| CADL Concept | IEC 62853 Concept |
|---|---|
| alpha (information sharing) | Information Transparency Level |
| beta (authority centralization) | Governance Centralization Index |
| lambda (incentive alignment) | Stakeholder Alignment Metric |
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
| beta | 0.8 (centralized) | 0.1 (decentralized) |
| Central Planner | ECBS | aggregation_only |
| Local Planner | tracking_only | LRA* + conflict avoidance |
| Sharing Mode | uplink + broadcast | peer-to-peer broadcast |

#### Raspimouse Swarm Simulator — D-SoS / C-SoS / MCP-SoS Comparison

Three CADL definitions model the same 5-robot swarm on an 11-node graph network under different SoS paradigms. These connect to the raspimouse-swarm-simulator (not publicly available at present) via the Unity config generator (`cadl sim-gen --target unity`).

| Property | D-SoS (Directed) | C-SoS (Collaborative) | MCP-SoS (Acknowledged) |
|---|---|---|---|
| Decision Authority | ARBITRATOR | ROBOT[*] (propose) + ARBITRATOR (verify) | LLM_AGENT |
| beta | 0.9 | 0.3 | 0.6 |
| alpha | 0.2 | 0.7 | 0.9 |
| Central Planner | NaiveDijkstra | DirectionDijkstra | LLM_Dijkstra |
| Local Planner | none | DirectionDijkstra | NaiveDijkstra + OccupancyAware |
| Communication | NATS req/res | NATS req/res + resource query | MCP tools |
| Regimes | NORMAL ↔ CONGESTED | NORMAL ↔ CONGESTED | NORMAL ↔ COLLISION_RESOLUTION ↔ DEADLOCK |

```bash
# Generate Unity configs for all three modes
cadl sim-gen examples/raspimouse_d_sos.cadl --target unity -o output/raspimouse_d_sos_unity.json
cadl sim-gen examples/raspimouse_c_sos.cadl --target unity -o output/raspimouse_c_sos_unity.json
cadl sim-gen examples/raspimouse_mcp_sos.cadl --target unity -o output/raspimouse_mcp_sos_unity.json
```

### AI Integration

`cadl ai` generates CADL from natural language descriptions via Claude API:

1. User provides an NL description (Japanese or English)
2. Claude generates a CADL definition using few-shot examples
3. The generated CADL is parsed and type-checked
4. On validation errors, the system retries with error feedback

Requires `ANTHROPIC_API_KEY` environment variable and `pip install "cadl-lang[ai]"`.

### Type Checks

The type checker validates:

1. **Actor reference existence** — All referenced actors are defined
2. **Contract party consistency** — No duplicate or missing parties
3. **Protocol step validity** — Senders and receivers match actor definitions
4. **Information sharing coherence** — Sharing declarations reference valid actors
5. **Parameter range constraints** — `0 <= alpha, beta, lambda <= 1`

## Hands-on

A 90-minute self-paced workshop (or a 5-session exercise course) walks you through the complete CADL toolchain end-to-end on a robot delivery System of Systems: **CADL modelling → SoS-DSL contracts (lifecycle + monitors) → visualisation → code generation → live simulation**.

The hands-on materials include:

- **Main textbook** — six 15-minute steps, with full bilingual EN / JA pages and a runnable end-to-end script (`scripts/sos_dsl_handson_e2e.sh`).
- **Exercises booklet** — a 5-session structured course (compact 3-session version available) with graded ★ / ★★ / ★★★ tasks and a rubric.
- **Academic background** — Maier's five SoS criteria, the **ISO/IEC/IEEE 21839 / 21840 / 21841** standards, related research (ADLs, Normative MAS, Runtime Verification), and an annotated bibliography.

| Audience | Entry point |
| --- | --- |
| Self-learner — quick tour | [Course A — Robot Delivery (main textbook)](https://ertlnagoya.github.io/cadl-spec/docs/handson/main-textbook) (EN) / [JA](https://ertlnagoya.github.io/cadl-spec/ja/docs/handson/main-textbook) |
| Student in a class | [Course A — Exercises](https://ertlnagoya.github.io/cadl-spec/docs/handson/exercises) (EN) / [JA](https://ertlnagoya.github.io/cadl-spec/ja/docs/handson/exercises) |
| Researcher needing citations | [Why SoS-DSL? (academic background)](https://ertlnagoya.github.io/cadl-spec/docs/handson/academic-background) (EN) / [JA](https://ertlnagoya.github.io/cadl-spec/ja/docs/handson/academic-background) |

The materials are hosted in the [cadl-spec repository](https://github.com/ertlnagoya/cadl-spec) — English sources under `docs/handson/`, Japanese under `i18n/ja/docusaurus-plugin-content-docs/current/handson/` — and rendered on the [Hands-on section of the spec website](https://ertlnagoya.github.io/cadl-spec/docs/handson/).

To run the full pipeline locally on the bundled robot-delivery example:

```bash
./scripts/sos_dsl_handson_e2e.sh
```

This generates the IR JSON, the Unity C# tree, and drops the latter into the `raspimouse-swarm-simulator` Unity project. See the hands-on textbook for the rest of the walkthrough.

## Examples

### CADL Definitions

| File | Description | SoS Type | Key Features |
|---|---|---|---|
| `robot_delivery.cadl` | Autonomous delivery fleet | Acknowledged | Route coordination, failure replanning, 2-regime transitions |
| `smart_city_traffic.cadl` | Traffic signal management | Collaborative | 3-regime transitions (NORMAL/CONGESTED/EMERGENCY), emergency override |
| `supply_chain.cadl` | Manufacturing supply chain | Collaborative | 4-actor chain, quality recall protocol, 5-regime transitions |
| `iot_data_sharing.cadl` | IoT sensor aggregation | Virtual | Data freshness contracts, privacy policy, anomaly detection |
| `household_chores.cadl` | Family chore sharing | Collaborative | Human-centric, monetary incentives, dispute resolution |
| `a_sos_robot_delivery.cadl` | MAPF robot delivery (A-SoS) | Acknowledged | Central ECBS planner, 3-regime transitions, fleet safety |
| `c_sos_taxi_fleet.cadl` | Autonomous taxi fleet (C-SoS) | Collaborative | Decentralized LRA*, peer conflict resolution, 5-regime transitions |
| `raspimouse_d_sos.cadl` | Raspimouse swarm (D-SoS) | Directed | Centralized arbitration via NATS, NaiveDijkstra, beta=0.9 |
| `raspimouse_c_sos.cadl` | Raspimouse swarm (C-SoS) | Collaborative | Local DirectionDijkstra + central verification, discrete-time sync |
| `raspimouse_mcp_sos.cadl` | Raspimouse swarm (MCP-SoS) | Acknowledged | LLM-controlled via MCP tools, Static/Dynamic path modes, 3 regimes |

### Demo Scripts

Run the demo scripts to see the full toolchain in action:

```bash
# End-to-end workflow: parse -> verify -> codegen (Python/Solidity/Rego) -> regime map -> IEC 62853
python examples/demo_robot_delivery.py

# Regime transitions, compliance analysis, and multi-target generation
python examples/demo_smart_city.py

# Side-by-side comparison of Python, Solidity, and Rego output
python examples/demo_codegen_targets.py
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
  iec62853.py          IEC 62853 compliance mapping
  unparse.py           Expression AST back to CADL source text
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
  test_iec62853.py       IEC 62853 compliance mapping tests
  test_sim_ir.py         Simulator IR lowering and validation tests
  test_sim_gen.py        Simulator config generator tests

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
| `model_check` / `simulation` / `proof` verification methods | Not supported yet (reported explicitly) |

## Related Projects

- raspimouse-swarm-simulator (not publicly available at present) — Multi-agent swarm robotics simulation platform. CADL files in `examples/raspimouse_*.cadl` describe its three SoS modes, and the Unity config generator produces configuration JSON for the simulator.

## References

- Benveniste et al., "Contracts for System Design," Foundations and Trends in EDA, 2018.
- ISO/IEC/IEEE 21841:2019, Taxonomy of Systems of Systems.
- Saoud et al., "Assume-guarantee contracts for continuous-time systems," Automatica, 2021.
- IEC 62853:2018, Open Systems Dependability.

## License

[Apache License 2.0](LICENSE). Releases up to and including 0.3.2 were published under the MIT License.
