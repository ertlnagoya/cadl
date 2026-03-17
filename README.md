# CADL: Contract Architecture Description Language

English | [日本語](README_ja.md)

CADL is a domain-specific language for formally describing, verifying, and deploying institutional designs in Systems of Systems (SoS). It enables stakeholders — from engineers to citizens — to define governance rules, contracts, protocols, and incentive structures in a machine-readable, verifiable format.

CADL is developed at the Matsubara Laboratory, Graduate School of Informatics, Nagoya University.

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
- **beta** — Decision decentralization (0 = centralized, 1 = fully distributed)
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
        beta: 0.2
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
```

## Installation

Requires Python 3.9+.

```bash
pip install -e ".[dev]"
```

## Usage

```bash
# Parse a CADL file and display summary
cadl parse examples/robot_delivery.cadl

# Parse and run type checks
cadl check examples/robot_delivery.cadl

# Print the full AST
cadl parse examples/robot_delivery.cadl --ast

# Full verification (type check + SMT + deadlock detection)
cadl verify examples/robot_delivery.cadl
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
    (Phase 2+)
         |
    +----+----+----------+
    v         v          v
  SMT       Code       Runtime
  Verify    Generate   Monitor
```

### Type Checks (Phase 1)

The type checker validates:

1. **Actor reference existence** — All referenced actors are defined
2. **Contract party consistency** — No duplicate or missing parties
3. **Protocol step validity** — Senders and receivers match actor definitions
4. **Information sharing coherence** — Sharing declarations reference valid actors
5. **Parameter range constraints** — `0 <= alpha, beta, lambda <= 1`

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
  cli.py               Command-line interface

tests/
  test_parser.py       Parser tests
  test_type_checker.py Type checker tests
  test_verifier.py     SMT verifier tests
  test_deadlock.py     Deadlock detector tests

examples/
  robot_delivery.cadl      Robot delivery SoS (Acknowledged type)
  household_chores.cadl    Family chore sharing (Collaborative type)
```

## Roadmap

| Phase | Goal | Status |
|---|---|---|
| 1 | Core language design, parser, type checker | Done |
| 2 | Verification engine (SMT-based consistency, deadlock detection) | Done |
| 3 | Runtime code generation (Python/TypeScript) | Planned |
| 4 | AI integration (NL-to-CADL via LLM) | Planned |
| 5 | Regime transitions and regime map construction | Planned |
| 6 | IEC 62853 integration, smart contract generation | Planned |

## References

- Benveniste et al., "Contracts for System Design," Foundations and Trends in EDA, 2018.
- ISO/IEC/IEEE 21841:2019, Taxonomy of Systems of Systems.
- Saoud et al., "Assume-guarantee contracts for continuous-time systems," Automatica, 2021.
- IEC 62853:2018, Open Systems Dependability.

## License

MIT
