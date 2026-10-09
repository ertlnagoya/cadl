"""CADL AI Prompts — system prompts and few-shot examples for NL-to-CADL generation."""

from __future__ import annotations

CADL_SYNTAX_REFERENCE = """\
# CADL Syntax Reference

CADL (Contract Architecture Description Language) uses YAML-like declarative syntax.
Each file describes one SoS (System of Systems) definition.

## Top-level structure

```yaml
sos:
  name: "SystemName"
  type: <SoS_Type>
  version: "1.0.0"
  description: "Optional description"

  actors:
    - id: ACTOR_NAME
      role: "role_name"
      autonomy: <autonomy_level>
      capabilities:
        - capability_1
        - capability_2

  contracts:
    - id: CONTRACT_NAME
      parties:
        - ACTOR1
        - "ACTOR2[*]"
      assume:
        - "condition_expression"
      guarantee:
        - "guarantee_expression"
      authority:
        decision_holder: ACTOR1
        beta: 0.8
      information:
        alpha: 0.8
      incentives:
        type: <incentive_type>
        lambda: 0.5
        rules:
          - "reward(ACTOR[i], amount) when condition"
          - "penalty(ACTOR[i], amount) when condition"
      duration: indefinite

  protocols:
    - id: PROTOCOL_NAME
      trigger: "trigger_expression"
      steps:
        - "SENDER -> RECEIVER : message_name"
        - "ACTOR : local_computation"
      timing:
        max_total: 500ms
      fallback:
        on_timeout: "ACTOR : fallback_action()"

  metrics:
    - id: metric_name
      formula: "computation_expression"
      target: ">= 0.95"
```

## SoS Types
- Directed: Central authority controls all constituent systems
- Acknowledged: Central coordinator exists but systems retain autonomy
- Collaborative: Peer systems cooperate without central authority
- Virtual: Loosely coupled systems with minimal coordination

## Autonomy Levels
- none, low, medium, high, full

## Institutional Parameters (all in range [0, 1])
- alpha: Information sharing degree (0=local only, 1=full sharing)
- beta: Decision centralization (0=fully distributed, 1=centralized)
- lambda: Incentive strength (0=directive-based, 1=market mechanism)

## Actor References
- Simple: ACTOR_NAME (e.g., DISPATCHER)
- Parameterized: "ACTOR[1..N]" (e.g., "ROBOT[1..N]")
- Wildcard: "ACTOR[*]" (e.g., "ROBOT[*]" means all robots)
- Indexed: "ACTOR[i]" (e.g., "ROBOT[i]" in quantified expressions)

## Protocol Steps
- Message: "SENDER -> RECEIVER : message_name"
- Broadcast: "SENDER -> RECEIVER[*] : message_name"
- Compute: "ACTOR : computation_name"

## Expression Syntax
- Comparisons: ==, !=, <, <=, >, >=
- Logical: AND, OR, NOT
- Arithmetic: +, -, *, /
- Duration literals: 100ms, 5s, 30min, 1h
- Member access: ACTOR.property
- Function calls: function_name(args)
- Quantifiers: for all x in domain : predicate, exists x in domain : predicate

## Incentive Types
- reputation, monetary, penalty, reward

## Duration Values
- indefinite, or duration literal (e.g., "30 days", "1 year")
"""

EXAMPLE_ROBOT_DELIVERY = """\
sos:
  name: "RobotDeliverySystem"
  type: Acknowledged
  version: "1.0.0"

  actors:
    - id: DISPATCHER
      role: "global_planner"
      autonomy: low
      capabilities:
        - compute_routes
        - monitor_all

    - id: "ROBOT[1..N]"
      role: "delivery_vehicle"
      autonomy: high
      capabilities:
        - local_navigation
        - obstacle_detection

    - id: "CUSTOMER[1..M]"
      role: "service_user"
      autonomy: medium

  contracts:
    - id: DELIVERY_SLA
      parties:
        - DISPATCHER
        - "ROBOT[*]"
        - "CUSTOMER[*]"
      assume:
        - "DISPATCHER.is_operational == true"
        - "network_latency <= 200ms"
      guarantee:
        - "all_routes_conflict_free()"
        - "delivery_time <= promised_time * 1.2"
      authority:
        decision_scope: "route_assignment"
        decision_holder: DISPATCHER
        beta: 0.8
      information:
        alpha: 0.8
        views:
          DISPATCHER: "global_map + all_robot_positions"
          "ROBOT[*]": "assigned_route + local_sensors"
        sharing:
          - "ROBOT[*] -> DISPATCHER : position"
          - "DISPATCHER -> ROBOT[*] : route"
      responsibilities:
        DISPATCHER:
          - "compute_conflict_free_routes(all_robots)"
          - "reassign_on_failure"
        "ROBOT[*]":
          - "follow_assigned_route"
          - "report_position(period: 1s)"
      incentives:
        type: reputation
        lambda: 0.5
        rules:
          - "reward(ROBOT[i], 10) when on_time_delivery"
          - "penalty(ROBOT[i], -5) when route_deviation"
      violation:
        detect: "route_deviation > threshold OR position_report_delay > 5s"
        action: "notify(DISPATCHER) AND log_violation"
        escalation: "after 3 violations: suspend_contract"
      duration: indefinite

  protocols:
    - id: FAILURE_REPLAN
      trigger: "road_failure_detected_by(ROBOT[i])"
      precondition: "DISPATCHER.is_operational"
      steps:
        - "ROBOT[i] -> DISPATCHER : failure_report"
        - "DISPATCHER : validate_failure"
        - "DISPATCHER : recompute_routes"
        - "DISPATCHER -> ROBOT[*] : new_route"
      timing:
        max_response: 100ms
        max_total: 500ms
      fallback:
        on_timeout: "ROBOT[*] : execute_safe_stop()"
        on_failure: "switch_to_protocol(LOCAL_REPLAN)"
      postcondition: "all_routes_conflict_free()"

  metrics:
    - id: delivery_success_rate
      formula: "count(on_time_deliveries) / count(total_deliveries)"
      target: ">= 0.95"
    - id: avg_delivery_time
      formula: "mean(delivery_times)"
      target: "<= 30min"
"""

EXAMPLE_HOUSEHOLD_CHORES = """\
sos:
  name: "FamilyHousehold"
  type: Collaborative
  version: "1.0.0"
  description: "Family chore sharing rules for 4 members"

  actors:
    - id: "PARENT[1..2]"
      role: "parent"
      autonomy: high
    - id: "CHILD[1..2]"
      role: "child"
      autonomy: medium

  contracts:
    - id: CHORE_SHARING
      parties:
        - "PARENT[*]"
        - "CHILD[*]"
      assume:
        - "CHILD[i].age >= 6"
      guarantee:
        - "all_chores_assigned_weekly()"
      authority:
        decision_holder: "PARENT[1]"
        beta: 0.7
      information:
        alpha: 1.0
      responsibilities:
        "PARENT[*]":
          - "assign_weekly_chores"
          - "verify_completion"
        "CHILD[*]":
          - "complete_assigned_chores_before_dinner"
      incentives:
        type: monetary
        lambda: 0.6
        rules:
          - "reward(CHILD[i], 100) when chore_completed"
          - "penalty(CHILD[i], -50) when chore_missed"
      duration: indefinite

  protocols:
    - id: WEEKLY_ASSIGNMENT
      trigger: "every_monday_morning()"
      steps:
        - "PARENT[1] : generate_chore_list"
        - "PARENT[1] -> CHILD[*] : chore_assignments"
      timing:
        max_total: 1h

    - id: DISPUTE_RESOLUTION
      trigger: "dispute_raised_by(CHILD[i])"
      steps:
        - "CHILD[i] -> PARENT[*] : dispute_report"
        - "PARENT[*] : discuss_resolution"
        - "PARENT[1] -> CHILD[*] : final_decision"
      fallback:
        on_timeout: "PARENT[1] : make_final_decision()"
"""


def build_system_prompt() -> str:
    """Build the system prompt for NL-to-CADL generation."""
    return f"""\
You are a CADL (Contract Architecture Description Language) expert. Your task is to generate \
valid CADL definitions from natural language descriptions of Systems of Systems (SoS).

{CADL_SYNTAX_REFERENCE}

## Examples

### Example 1: Robot Delivery System (Acknowledged SoS)

Input: "A robot delivery system where a central dispatcher plans routes for N robots. \
Robots have high autonomy for local navigation. There's an SLA contract ensuring \
on-time delivery with reputation-based incentives."

Output:
```
{EXAMPLE_ROBOT_DELIVERY}
```

### Example 2: Family Household Chores (Collaborative SoS)

Input: "A family chore sharing system with 2 parents and 2 children. Parents assign \
weekly chores. Children earn allowance for completing chores. There's a dispute \
resolution protocol."

Output:
```
{EXAMPLE_HOUSEHOLD_CHORES}
```

## Rules

1. Output ONLY valid CADL (YAML format). No explanations, no markdown fences.
2. Always include the top-level `sos:` key.
3. Choose the appropriate SoS type based on the governance structure described.
4. Set institutional parameters (alpha, beta, lambda) to reflect the described power dynamics.
5. Include at least one contract with assume/guarantee clauses.
6. Include at least one protocol if coordination procedures are described.
7. Use parameterized actors (e.g., "ROBOT[1..N]") when multiple instances are implied.
8. Use realistic expression syntax in assume/guarantee/trigger fields.
9. Ensure all actor references in contracts and protocols match defined actors.
10. Support both Japanese and English input — generate CADL with English identifiers regardless of input language.
"""


def build_user_prompt(description: str) -> str:
    """Build the user prompt for NL-to-CADL generation."""
    return f"""\
Generate a CADL definition for the following system:

{description}

Output only the CADL definition (valid YAML starting with "sos:"). No explanations or markdown."""


def build_retry_prompt(cadl_source: str, errors: list[str]) -> str:
    """Build a retry prompt with error feedback."""
    error_list = "\n".join(f"- {e}" for e in errors)
    return f"""\
The following CADL definition has errors:

```
{cadl_source}
```

Errors:
{error_list}

Please fix these errors and output a corrected CADL definition. \
Output only the CADL definition (valid YAML starting with "sos:"). No explanations or markdown."""
