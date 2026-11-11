"""CADL Command-Line Interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cadl",
        description="CADL: Contract Architecture Description Language toolchain",
    )
    parser.add_argument("--version", action="version", version=f"cadl {__version__}")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # parse command
    parse_cmd = subparsers.add_parser("parse", help="Parse a CADL file and report errors")
    parse_cmd.add_argument("file", type=Path, help="CADL file to parse")
    parse_cmd.add_argument("--ast", action="store_true", help="Print AST")

    # check command
    check_cmd = subparsers.add_parser("check", help="Parse and type-check a CADL file")
    check_cmd.add_argument("file", type=Path, help="CADL file to check")

    # verify command
    verify_cmd = subparsers.add_parser("verify", help="Parse, type-check, and verify a CADL file")
    verify_cmd.add_argument("file", type=Path, help="CADL file to verify")
    verify_cmd.add_argument("--format", choices=["text", "json"], default="text",
                             help="Output format (default: text)")

    # codegen command
    codegen_cmd = subparsers.add_parser("codegen", help="Generate runtime code from a CADL file")
    codegen_cmd.add_argument("file", type=Path, help="CADL file to generate from")
    codegen_cmd.add_argument("--target", "-t",
                             choices=["python", "solidity", "opa", "unity-csharp",
                                      "agent-rego", "agent-cedar"],
                             default="python",
                             help="Code generation target (default: python); "
                                  "agent-rego and agent-cedar generate the boundary "
                                  "policy of the agent governance extension "
                                  "(agents 0.1)")
    codegen_cmd.add_argument("--output", "-o", type=Path, default=Path("generated"),
                             help="Output directory (default: generated/)")

    # agents-decide command
    decide_cmd = subparsers.add_parser(
        "agents-decide",
        help="Decide one message at the boundary: allow, deny or escalate "
             "(agent governance extension, agents 0.1)",
        description="Reference decision function of the agent governance "
                    "extension (Appendix G, G.5) for one message. The policy input "
                    "(G.10) has 'message' (from, to, name) and optionally 'now' and "
                    "'instances' (bindings, in_force, derived). Stateless: holds, "
                    "grants and revocations belong to a host runtime. Exit status: "
                    "0 allow, 1 deny, 3 escalate, 2 usage error.")
    decide_cmd.add_argument("file", type=Path, help="CADL file with enforced contracts")
    decide_cmd.add_argument("--input", "-i", type=Path, required=True,
                            help="Policy input as a JSON file ('-' for stdin)")
    decide_cmd.add_argument("--format", choices=["text", "json"], default="text",
                            help="Output format (default: text)")

    # agents-run command
    run_cmd = subparsers.add_parser(
        "agents-run",
        help="Replay a recorded sequence of messages through the enforcement "
             "point: decisions, holds, delegations and audit records "
             "(agent governance extension, agents 0.1)",
        description="Reference runtime of the agent governance extension "
                    "(Appendix G, G.5 to G.8). It decides every message of the "
                    "trace with the decision function of `agents-decide`, on the "
                    "state left by the earlier messages: in-force flags from the "
                    "norms engine, derived instances from grants and revocations, "
                    "holds with approval, rejection and timeout. Deterministic "
                    "and offline: the only clock is 't' of the trace. Exit "
                    "status: 0 when the trace was replayed, 2 usage error.")
    run_cmd.add_argument("file", type=Path, help="CADL file with enforced contracts")
    run_cmd.add_argument("--trace", type=Path, required=True,
                         help="Recorded messages (JSON Lines, the trace format of "
                              "norms-eval with 'epoch' and 'instances')")
    run_cmd.add_argument("--format", choices=["text", "json"], default="text",
                         help="Output format (default: text)")
    run_cmd.add_argument("--audit", type=Path, default=None, metavar="OUT.jsonl",
                         help="Write the audit records (G.8) as JSON Lines")

    # regime-map command
    regime_cmd = subparsers.add_parser("regime-map", help="Analyze regime transitions and build a regime map")
    regime_cmd.add_argument("file", type=Path, help="CADL file to analyze")
    regime_cmd.add_argument("--format", choices=["text", "dot", "json"], default="text",
                             help="Output format (default: text)")
    regime_cmd.add_argument("--output", "-o", type=Path, default=None,
                             help="Save output to file")

    # iec62853 command
    iec_cmd = subparsers.add_parser("iec62853", help="Generate an IEC 62853-oriented dependability summary (informative; not a conformance assessment)")
    iec_cmd.add_argument("file", type=Path, help="CADL file to analyze")
    iec_cmd.add_argument("--format", choices=["text", "json"], default="text",
                         help="Output format (default: text)")
    iec_cmd.add_argument("--output", "-o", type=Path, default=None,
                         help="Save output to file")

    # sim-validate command
    sim_val_cmd = subparsers.add_parser("sim-validate",
        help="Parse CADL, lower to simulator IR, and validate")
    sim_val_cmd.add_argument("file", type=Path, help="CADL file to validate")

    # sim-ir command
    sim_ir_cmd = subparsers.add_parser("sim-ir",
        help="Print the 3-layer simulator IR for a CADL file")
    sim_ir_cmd.add_argument("file", type=Path, help="CADL file to lower")
    sim_ir_cmd.add_argument("--format", choices=["yaml", "json"], default="yaml",
                            help="Output format (default: yaml)")

    # sim-gen command
    sim_gen_cmd = subparsers.add_parser("sim-gen",
        help="Generate simulator config from a CADL file")
    sim_gen_cmd.add_argument("file", type=Path, help="CADL file to generate from")
    sim_gen_cmd.add_argument("--target", "-t", choices=["python", "unity", "go"],
                             required=True, help="Simulator target")
    sim_gen_cmd.add_argument("--output", "-o", type=Path, default=None,
                             help="Output file (default: stdout)")

    # norms-eval command
    norms_cmd = subparsers.add_parser("norms-eval",
        help="Compute the verdicts of a norms 0.1 contract on a recorded trace (Appendix F, F.5)")
    norms_cmd.add_argument("file", type=Path, help="CADL file that declares norms: 0.1")
    norms_cmd.add_argument("--trace", type=Path, required=True,
                           help="Timed trace of one contract instance (JSON Lines)")
    norms_cmd.add_argument("--contract", default=None, metavar="ID",
                           help="Contract to evaluate (default: the only one with norms)")
    norms_cmd.add_argument("--format", choices=["text", "json"], default="text",
                           help="Output format (default: text)")

    # norms-diff command
    diff_cmd = subparsers.add_parser("norms-diff",
        help="Report what an amendment from OLD to NEW does to the norms (Appendix F, F.6)")
    diff_cmd.add_argument("old", type=Path, help="Old version of the CADL file")
    diff_cmd.add_argument("new", type=Path, help="New version of the CADL file")
    diff_cmd.add_argument("--contract", default=None, metavar="ID",
                          help="Contract to compare (default: every contract with norms)")
    diff_cmd.add_argument("--format", choices=["text", "json"], default="text",
                          help="Output format (default: text)")

    # model-export command (stochastic extension, Appendix H)
    mexp_cmd = subparsers.add_parser("model-export",
        help="Export the model: block (stochastic 0.1) to PRISM, one model file "
             "and one properties file per design")
    mexp_cmd.add_argument("file", type=Path, help="CADL file to export from")
    mexp_cmd.add_argument("--output", "-o", type=Path, default=Path("."),
                          help="Output directory (default: current directory)")
    mexp_cmd.add_argument("--regime", default=None,
                          help="Export only the design of this regime")

    # model-check command (stochastic extension, Appendix H)
    mchk_cmd = subparsers.add_parser("model-check",
        help="Evaluate the properties of the model: block (stochastic 0.1) with the "
             "internal reference evaluator (exact arithmetic; not a model checker), "
             "or compute a certified regime map (--regime-map ID --certify)")
    mchk_cmd.add_argument("file", type=Path, help="CADL file to evaluate")
    mchk_cmd.add_argument("--regime", default=None,
                          help="Evaluate only the design of this regime")
    mchk_cmd.add_argument("--set", dest="assignments", action="append", default=[],
                          metavar="p=v", help="Give parameter p the value v (a decimal "
                          "or n/d) instead of its fixed value; may be repeated")
    mchk_cmd.add_argument("--regime-map", dest="regime_map", default=None, metavar="ID",
                          help="Evaluate this regime map query: at sample points "
                               "(estimates; the default) or, with --certify, as a "
                               "certified regime map")
    mchk_cmd.add_argument("--grid", type=int, default=None, metavar="N",
                          help="With --regime-map: intervals per open parameter of the "
                               "uniform grid whose corner points are evaluated (default: 4)")
    mchk_cmd.add_argument("--certify", action="store_true",
                          help="With --regime-map: compute the certified regime map "
                               "(cells decided by parameter lifting with exact arithmetic)")
    mchk_cmd.add_argument("--max-depth", dest="max_depth", type=int, default=None, metavar="N",
                          help="With --certify: bisect a cell at most N times (default: 24)")
    mchk_cmd.add_argument("--max-cells", dest="max_cells", type=int, default=None, metavar="N",
                          help="With --certify: refine the partition to at most N cells "
                               "(default: 20000)")
    mchk_cmd.add_argument("--format", choices=["text", "json"], default="text",
                          help="Output format (default: text)")

    # ai command
    ai_cmd = subparsers.add_parser("ai", help="Generate CADL from natural language description")
    ai_cmd.add_argument("description", nargs="?", default=None,
                        help="Natural language description of the SoS")
    ai_cmd.add_argument("-f", "--file", type=Path, dest="input_file",
                        help="Read description from a file")
    ai_cmd.add_argument("-o", "--output", type=Path, dest="output_file",
                        help="Save generated CADL to file")
    ai_cmd.add_argument("--verify", action="store_true",
                        help="Run SMT verification and deadlock detection on generated CADL")
    ai_cmd.add_argument("--model", type=str, default=None,
                        help="Override LLM model (default: $CADL_LLM_MODEL, else claude-sonnet-5-5)")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "parse":
        return _cmd_parse(args)
    elif args.command == "check":
        return _cmd_check(args)
    elif args.command == "verify":
        return _cmd_verify(args)
    elif args.command == "codegen":
        return _cmd_codegen(args)
    elif args.command == "agents-decide":
        return _cmd_agents_decide(args)
    elif args.command == "regime-map":
        return _cmd_regime_map(args)
    elif args.command == "iec62853":
        return _cmd_iec62853(args)
    elif args.command == "sim-validate":
        return _cmd_sim_validate(args)
    elif args.command == "sim-ir":
        return _cmd_sim_ir(args)
    elif args.command == "sim-gen":
        return _cmd_sim_gen(args)
    elif args.command == "agents-run":
        return _cmd_agents_run(args)
    elif args.command == "norms-eval":
        return _cmd_norms_eval(args)
    elif args.command == "norms-diff":
        return _cmd_norms_diff(args)
    elif args.command == "model-export":
        return _cmd_model_export(args)
    elif args.command == "model-check":
        return _cmd_model_check(args)
    elif args.command == "ai":
        return _cmd_ai(args)

    return 0


def _cmd_parse(args: argparse.Namespace) -> int:
    from .parser import parse_file

    path = args.file
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 1

    try:
        sos = parse_file(path)
    except Exception as e:
        print(f"Parse error: {e}", file=sys.stderr)
        return 1

    print(f"Successfully parsed: {path}")
    print(f"  SoS name: {sos.name}")
    if sos.type:
        print(f"  Type: {sos.type.value}")
    if sos.version:
        print(f"  Version: {sos.version}")
    print(f"  Actors: {len(sos.actors)}")
    print(f"  Contracts: {len(sos.contracts)}")
    print(f"  Protocols: {len(sos.protocols)}")
    print(f"  Metrics: {len(sos.metrics)}")

    if args.ast:
        import pprint
        pprint.pprint(sos)

    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    from .parser import parse_file
    from .type_checker import type_check

    path = args.file
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 1

    try:
        sos = parse_file(path)
    except Exception as e:
        print(f"Parse error: {e}", file=sys.stderr)
        return 1

    result = type_check(sos)

    for info in result.infos:
        print(f"  {info}", file=sys.stderr)
    for warning in result.warnings:
        print(f"  {warning}", file=sys.stderr)
    for error in result.errors:
        print(f"  {error}", file=sys.stderr)

    if result.ok:
        print(f"Type check passed: {path}")
        if result.warnings:
            print(f"  ({len(result.warnings)} warning(s))")
        return 0
    else:
        print(f"Type check failed: {len(result.errors)} error(s), {len(result.warnings)} warning(s)")
        return 1


def _cmd_verify(args: argparse.Namespace) -> int:
    from .parser import parse_file
    from .type_checker import type_check
    from .verifier import verify
    from .deadlock import detect_deadlocks

    path = args.file
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 1

    # Parse
    try:
        sos = parse_file(path)
    except Exception as e:
        print(f"Parse error: {e}", file=sys.stderr)
        return 1

    # Run checks
    tc_result = type_check(sos)
    v_results = verify(sos)
    d_results = detect_deadlocks(sos)

    has_failures = not tc_result.ok or \
        any(r.status == "failed" for r in v_results) or \
        any(r.status == "failed" for r in d_results)

    # "info" and "not_supported" results are findings, not checks with a
    # verdict, so they are left out of the pass/fail totals.
    judged = ("passed", "failed", "unknown", "warning")
    total_checks = sum(1 for r in v_results if r.status in judged) + \
                   sum(1 for r in d_results if r.status in judged) + \
                   1  # +1 for type check
    passed = sum(1 for r in v_results if r.status == "passed") + \
             sum(1 for r in d_results if r.status == "passed") + \
             (1 if tc_result.ok else 0)
    failed = sum(1 for r in v_results if r.status == "failed") + \
             sum(1 for r in d_results if r.status == "failed") + \
             (0 if tc_result.ok else 1)

    # JSON output
    if args.format == "json":
        import json
        output = {
            "file": str(path),
            "sos": sos.name,
            "type_check": {
                "ok": tc_result.ok,
                "errors": [str(e) for e in tc_result.errors],
                "warnings": [str(w) for w in tc_result.warnings],
                "infos": [str(i) for i in tc_result.infos],
            },
            "verification": [
                {"name": r.check_name, "status": r.status, "message": r.message,
                 "counterexample": r.counterexample}
                for r in v_results
            ],
            "deadlock": [
                {"name": r.check_name, "status": r.status, "message": r.message,
                 "details": getattr(r, 'details', None)}
                for r in d_results
            ],
            "summary": {
                "total": total_checks,
                "passed": passed,
                "failed": failed,
            }
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return 1 if has_failures else 0

    # Text output
    print(f"Verifying: {path}")
    print(f"  SoS: {sos.name}")
    print()

    print("--- Type Check ---")
    for i in tc_result.infos:
        print(f"  {i}")
    for w in tc_result.warnings:
        print(f"  {w}")
    for e in tc_result.errors:
        print(f"  {e}")
    if tc_result.ok:
        print("  [PASS] Type check passed")
    else:
        print(f"  [FAIL] {len(tc_result.errors)} error(s)")
    print()

    print("--- SMT Verification ---")
    for r in v_results:
        print(f"  {r}")
    if not v_results:
        print("  (no contracts to verify)")
    print()

    print("--- Deadlock Detection ---")
    for r in d_results:
        print(f"  {r}")
    if not d_results:
        print("  (no protocols to analyze)")
    print()

    if has_failures:
        print(f"Verification FAILED: {passed}/{total_checks} checks passed")
        return 1
    else:
        print(f"Verification PASSED: {passed}/{total_checks} checks passed")
        return 0


def _cmd_codegen(args: argparse.Namespace) -> int:
    from .parser import parse_file
    from .type_checker import type_check
    from .codegen import generate, omitted_temporal_items

    path = args.file
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 1

    # Parse
    try:
        sos = parse_file(path)
    except Exception as e:
        print(f"Parse error: {e}", file=sys.stderr)
        return 1

    # Type check
    tc_result = type_check(sos)
    for w in tc_result.warnings:
        print(f"  {w}", file=sys.stderr)
    if not tc_result.ok:
        for e in tc_result.errors:
            print(f"  {e}", file=sys.stderr)
        print(f"Type check failed: cannot generate code", file=sys.stderr)
        return 1

    # Generate
    target = args.target
    output_dir = args.output
    for contract_id, key, text in omitted_temporal_items(sos):
        print(f"  [WARNING]: {key} item of contract '{contract_id}' has a temporal "
              f"operator (norms 0.1) and is not generated: {text}", file=sys.stderr)
    try:
        generate(sos, output_dir, target=target)
    except Exception as e:
        print(f"Code generation error: {e}", file=sys.stderr)
        return 1

    print(f"Code generated ({target}): {output_dir}/")
    print(f"  SoS: {sos.name}")
    print(f"  Actors: {len(sos.actors)}")
    print(f"  Contracts: {len(sos.contracts)}")
    print(f"  Protocols: {len(sos.protocols)}")
    if target == "python":
        print(f"  Metrics: {len(sos.metrics)}")
    return 0


# Exit status of `cadl agents-decide`, by decision; 2 is a usage error.
_DECISION_STATUS = {"allow": 0, "deny": 1, "escalate": 3}


def _cmd_agents_decide(args: argparse.Namespace) -> int:
    import json
    from .agents import AgentsInputError, build_policy, decide, format_result
    from .parser import parse_file
    from .type_checker import type_check

    def usage_error(message: str) -> int:
        print(f"Error: {message}", file=sys.stderr)
        return 2

    path = args.file
    if not path.exists():
        return usage_error(f"File not found: {path}")
    try:
        sos = parse_file(path)
    except Exception as e:
        return usage_error(f"Parse error: {e}")
    tc_result = type_check(sos)
    if not tc_result.ok:
        for e in tc_result.errors:
            print(f"  {e}", file=sys.stderr)
        return usage_error("Type check failed: cannot decide")
    try:
        policy = build_policy(sos)
    except ValueError as e:
        return usage_error(str(e))

    try:
        if str(args.input) == "-":
            text = sys.stdin.read()
        else:
            text = args.input.read_text(encoding="utf-8")
        result = decide(policy, json.loads(text))
    except OSError as e:
        return usage_error(f"Cannot read the policy input: {e}")
    except json.JSONDecodeError as e:
        return usage_error(f"The policy input is not JSON: {e}")
    except AgentsInputError as e:
        return usage_error(f"Invalid policy input: {e}")

    if args.format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(format_result(result))
    return _DECISION_STATUS[result["decision"]]


def _cmd_regime_map(args: argparse.Namespace) -> int:
    import json as json_mod
    from .parser import parse_file
    from .regime_map import RegimeMap

    path = args.file
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 1

    try:
        sos = parse_file(path)
    except Exception as e:
        print(f"Parse error: {e}", file=sys.stderr)
        return 1

    rm = RegimeMap.from_sos(sos)

    if not rm.states:
        print("No transitions defined in this CADL file.", file=sys.stderr)
        return 0

    if args.format == "dot":
        output = rm.to_dot()
    elif args.format == "json":
        output = json_mod.dumps(rm.to_json(), indent=2, ensure_ascii=False)
    else:
        output = rm.to_text()

    if args.output:
        args.output.write_text(output, encoding="utf-8")
        print(f"Saved to: {args.output}", file=sys.stderr)
    else:
        print(output)

    return 0


def _cmd_iec62853(args: argparse.Namespace) -> int:
    import json as json_mod
    from .parser import parse_file
    from .iec62853 import generate_iec62853_report

    path = args.file
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 1

    try:
        sos = parse_file(path)
    except Exception as e:
        print(f"Parse error: {e}", file=sys.stderr)
        return 1

    report = generate_iec62853_report(sos)

    if args.format == "json":
        output = json_mod.dumps(report, indent=2, ensure_ascii=False)
    else:
        output = _format_iec62853_text(report)

    if args.output:
        args.output.write_text(output, encoding="utf-8")
        print(f"Saved to: {args.output}", file=sys.stderr)
    else:
        print(output)

    return 0


def _format_iec62853_text(report: dict) -> str:
    lines = []
    lines.append(f"Dependability Summary (IEC 62853-oriented): {report['sos_name']}")
    lines.append("=" * 60)
    if report.get("disclaimer"):
        lines.append(f"Note: {report['disclaimer']}")
    lines.append("")

    lines.append(f"Integration Level (CADL-defined): {report['system_integration_level']}")
    lines.append(f"SoS Type: {report['sos_type']}")
    lines.append("")

    lines.append("--- Institutional Parameters ---")
    for param in report.get("institutional_parameters", []):
        lines.append(f"  {param['cadl_concept']}: {param['value']}")
        lines.append(f"    Indicator (CADL-defined): {param['iec62853_concept']}")
    lines.append("")

    lines.append("--- Service Level Agreements ---")
    for sla in report.get("service_level_agreements", []):
        lines.append(f"  {sla['contract_id']}:")
        lines.append(f"    Assumptions: {sla['assumption_count']}")
        lines.append(f"    Guarantees: {sla['guarantee_count']}")
        if sla.get("failure_response"):
            lines.append(f"    Failure Response: {sla['failure_response']}")
    lines.append("")

    if report.get("operational_state_machine"):
        lines.append("--- Operational State Machine ---")
        osm = report["operational_state_machine"]
        lines.append(f"  States: {osm['state_count']}")
        lines.append(f"  Transitions: {osm['transition_count']}")
        if osm.get("initial_state"):
            lines.append(f"  Initial State: {osm['initial_state']}")
        lines.append("")

    lines.append("--- Dependability Summary ---")
    dep = report.get("dependability_summary", {})
    lines.append(f"  Total Contracts: {dep.get('total_contracts', 0)}")
    lines.append(f"  Total Actors: {dep.get('total_actors', 0)}")
    lines.append(f"  Governance Index (avg beta): {dep.get('governance_index', 'N/A')}")
    lines.append(f"  Transparency Level (avg alpha): {dep.get('transparency_level', 'N/A')}")
    lines.append(f"  Alignment Metric (avg lambda): {dep.get('alignment_metric', 'N/A')}")

    return "\n".join(lines)


def _cmd_sim_validate(args: argparse.Namespace) -> int:
    from .parser import parse_file
    from .sim import lower_to_ir, validate_ir

    path = args.file
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 1

    try:
        sos = parse_file(path)
    except Exception as e:
        print(f"Parse error: {e}", file=sys.stderr)
        return 1

    ir = lower_to_ir(sos)
    errors = validate_ir(ir)

    if errors:
        print(f"Validation FAILED for {path}:")
        for err in errors:
            print(f"  - {err}")
        return 1
    else:
        print(f"Validation OK: {path}")
        print(f"  SoS: {ir.name} ({ir.sos_type})")
        print(f"  Actors: {len(ir.institution.actors)}")
        print(f"  Contracts: {len(ir.institution.contracts)}")
        print(f"  Protocols: {len(ir.protocol.protocols)}")
        print(f"  Algorithms: {len(ir.algorithm.algorithms)}")
        print(f"  Transitions: {len(ir.transitions)}")
        return 0


def _cmd_sim_ir(args: argparse.Namespace) -> int:
    from .parser import parse_file
    from .sim import lower_to_ir
    from .sim.ir import SimIR

    path = args.file
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 1

    try:
        sos = parse_file(path)
    except Exception as e:
        print(f"Parse error: {e}", file=sys.stderr)
        return 1

    ir = lower_to_ir(sos)

    if args.format == "json":
        import json
        d = _ir_to_dict(ir)
        if "motivation_block" in d:
            d["motivation_block"] = _jsonable(d["motivation_block"])
        print(json.dumps(d, indent=2, ensure_ascii=False))
    else:
        import yaml
        print(yaml.dump(_ir_to_dict(ir), default_flow_style=False,
                        sort_keys=False, allow_unicode=True))

    return 0


def _ir_to_dict(ir) -> dict:
    """Serialize SimIR to a plain dict for YAML/JSON output."""
    from dataclasses import asdict
    d = asdict(ir)
    # The fields of the agent governance extension (Appendix G, G.13) are
    # part of the IR only for a file that declares it (G.14).
    if not d.pop("agents_extension", False):
        inst = d.get("institution", {})
        for a in inst.get("actors", []):
            for key in ("agent_kind", "agent_protocol", "agent_tools"):
                a.pop(key, None)
        for c in inst.get("contracts", []):
            for key in ("enforced", "enforcement_default", "enforcement_point",
                        "delegations"):
                c.pop(key, None)
            for n in c.get("norms", []):
                for key in ("on_denied_escalate_to", "on_denied_timeout_ms",
                            "on_denied_fallback", "on_denied_notify"):
                    n.pop(key, None)
    # Rename lambda_ back to lambda for readability
    for c in d.get("institution", {}).get("contracts", []):
        gov = c.get("governance", {})
        if "lambda_" in gov:
            gov["lambda"] = gov.pop("lambda_")
        # Likewise if_ of a counts-as rule (Appendix F, F.9)
        for rule in c.get("counts_as", []):
            if "if_" in rule:
                rule["if"] = rule.pop("if_")
    # The verbatim motivation block is an optional key: present only when
    # the source has a `motivation:` block.
    if d.get("motivation_block") is None:
        d.pop("motivation_block", None)
    return d


def _jsonable(value):
    """Make a YAML value safe for strict JSON (dates, sets, inf, nan → text)."""
    import math
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    return str(value)


def _cmd_sim_gen(args: argparse.Namespace) -> int:
    from .parser import parse_file
    from .sim import lower_to_ir, validate_ir, generate_config

    path = args.file
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 1

    try:
        sos = parse_file(path)
    except Exception as e:
        print(f"Parse error: {e}", file=sys.stderr)
        return 1

    ir = lower_to_ir(sos)
    errors = validate_ir(ir)
    if errors:
        print(f"Validation errors:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    config_str = generate_config(ir, args.target)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(config_str, encoding="utf-8")
        print(f"Generated {args.target} config: {args.output}")
    else:
        print(config_str)

    return 0


def _cmd_norms_eval(args: argparse.Namespace) -> int:
    """Exit status: 0 when no norm or guarantee is `violated` (`excused`
    and `pending` do not fail), 1 otherwise, 2 on a usage error."""
    from .norms_eval import NormsEvalError, TraceError, evaluate_files, format_text

    try:
        result = evaluate_files(args.file, args.trace, args.contract)
    except TraceError as e:
        print(f"Trace error: {args.trace}: {e}", file=sys.stderr)
        return 2
    except NormsEvalError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    if args.format == "json":
        import json
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(format_text(result))
    return 0 if result["ok"] else 1


def _cmd_agents_run(args: argparse.Namespace) -> int:
    """Exit status: 0 when the trace was replayed, whatever the decisions
    are; 2 on a usage error. The audit file is written only after the
    whole trace was replayed, so a failed run leaves no partial log."""
    import json
    from .agents_runtime import audit_lines, format_text, run_files
    from .norms_eval import NormsEvalError, TraceError

    def no_audit() -> None:
        if args.audit is not None:
            print(f"No audit file was written: the run did not complete "
                  f"({args.audit} was not created or changed).", file=sys.stderr)

    try:
        result = run_files(args.file, args.trace)
        log = "".join(line + "\n" for line in audit_lines(result))
        report = (json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)
                  if args.format == "json" else format_text(result))
    except TraceError as e:
        print(f"Trace error: {args.trace}: {e}", file=sys.stderr)
        no_audit()
        return 2
    except (NormsEvalError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        no_audit()
        return 2
    if args.audit is not None:
        try:
            args.audit.write_text(log, encoding="utf-8")
        except OSError as e:
            print(f"Error: cannot write the audit log: {e}", file=sys.stderr)
            return 2
    print(report)
    return 0


def _cmd_norms_diff(args: argparse.Namespace) -> int:
    """Exit status: 0 when nothing prevents the amendment, 1 on an
    error-level finding (a missing transform target, an invalid new
    version), 2 on a usage error."""
    from .norms_diff import diff_versions, format_diff_text
    from .norms_eval import NormsEvalError, load_version

    for path in (args.old, args.new):
        if not path.exists():
            print(f"Error: File not found: {path}", file=sys.stderr)
            return 2
    try:
        old = load_version(args.old)
    except NormsEvalError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    try:
        new = load_version(args.new)
    except NormsEvalError as e:
        # The new version cannot take effect: a finding, not a usage error.
        result = {"contracts": [], "invalid": [str(e)], "errors": [str(e)], "ok": False}
    else:
        try:
            result = diff_versions(old, new, args.contract)
        except NormsEvalError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 2
    result = {"old": str(args.old), "new": str(args.new), **result}

    if args.format == "json":
        import json
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(format_diff_text(result))
    return 0 if result["ok"] else 1


def _load_stochastic_model(path: Path):
    """Parse and type-check a file and return its interpreted model: block
    (Appendix H), or None after printing why there is none."""
    from .parser import parse_file
    from .stochastic import analyse, stochastic_declared
    from .type_checker import type_check

    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return None
    try:
        sos = parse_file(path)
    except Exception as e:
        print(f"Parse error: {e}", file=sys.stderr)
        return None
    tc_result = type_check(sos)
    for w in tc_result.warnings:
        print(f"  {w}", file=sys.stderr)
    if not tc_result.ok:
        for e in tc_result.errors:
            print(f"  {e}", file=sys.stderr)
        print("Type check failed: the model is not interpreted", file=sys.stderr)
        return None
    if sos.model is None:
        print(f"Error: {path} has no model: block", file=sys.stderr)
        return None
    if not stochastic_declared(sos):
        print(f"Error: {path} does not declare 'stochastic: 0.1' under extensions:; "
              f"its model: block is not interpreted (Appendix H, H.15)", file=sys.stderr)
        return None
    return analyse(sos).model


def _cmd_model_export(args: argparse.Namespace) -> int:
    from .stochastic import ExportError, export_prism

    model = _load_stochastic_model(args.file)
    if model is None:
        return 1
    try:
        files = export_prism(model, args.regime)
    except ExportError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    args.output.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (args.output / name).write_text(text, encoding="utf-8")
        print(f"Exported: {args.output / name}")
    return 0


def _print_certified_map(args: argparse.Namespace, model, result: dict) -> int:
    """Print a certified regime map (H.9). Exit status 0 when the
    resolution is reached, 1 when it is not."""
    from .stochastic import decimal_text
    from .stochastic_eval import value_text

    status = 0 if result["resolution_reached"] else 1
    stopped = {
        "resolution": "the resolution is reached",
        "max_depth": f"no undecided cell may be bisected again (--max-depth {result['max_depth']})",
        "max_cells": f"the partition has {result['cells_before_merging']} cells "
                     f"(--max-cells {result['max_cells']})",
    }[result["stopped"]]
    if args.format == "json":
        import json
        output = {
            "file": str(args.file),
            "sos": model.sos_name,
            "regime_map": {
                "id": result["id"],
                "certified": True,
                "note": result["note"],
                "method": result["method"],
                "direction": result["direction"],
                "objective": result["objective"],
                "constraint": result["constraint"],
                "open": result["open"],
                "regimes": result["regimes"],
                "region": {n: [decimal_text(lo), decimal_text(hi)]
                           for n, (lo, hi) in result["region"].items()},
                "resolution": decimal_text(result["resolution"]),
                "undecided_fraction": str(result["undecided_fraction"]),
                "undecided_fraction_decimal": value_text(result["undecided_fraction"]),
                "resolution_reached": result["resolution_reached"],
                "stopped": result["stopped"],
                "budget": {"max_depth": result["max_depth"], "max_cells": result["max_cells"]},
                "counts": {
                    **result["counts"],
                    "cells": len(result["cells"]),
                    "cells_before_merging": result["cells_before_merging"],
                    "cells_analysed": result["cells_analysed"],
                    "deepest_bisection": result["deepest"],
                },
                "volume_fractions": {k: str(v) for k, v in result["fractions"].items()},
                "volume_fraction_by_best": {k: str(v)
                                            for k, v in result["fraction_by_best"].items()},
                "cells": [
                    {
                        "box": {n: [decimal_text(lo), decimal_text(hi)]
                                for n, (lo, hi) in c.box.items()},
                        "status": c.status,
                        "best": list(c.best),
                        **({"reason": c.reason} if c.reason else {}),
                    }
                    for c in result["cells"]
                ],
            },
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return status

    counts = result["counts"]
    print(f"Model check: {args.file}")
    print(f"  SoS: {model.sos_name}")
    print()
    print(f"--- Regime map '{result['id']}': {result['note']} ---")
    print(f"  method: {result['method']}")
    print(f"  objective: {result['direction']} {result['objective']}")
    if result["constraint"]:
        print(f"  constraint: {result['constraint']}")
    print(f"  regimes: {', '.join(result['regimes'])}")
    region = ", ".join(f"{n} in [{decimal_text(lo)}, {decimal_text(hi)}]"
                       for n, (lo, hi) in result["region"].items())
    print(f"  region: {region}")
    print(f"  cells: {len(result['cells'])} ({counts['decided']} decided, "
          f"{counts['infeasible']} infeasible, {counts['undecided']} undecided); "
          f"{result['cells_analysed']} cell(s) analysed, deepest bisection {result['deepest']}")
    for best, share in result["fraction_by_best"].items():
        print(f"  decided, best = {best}: {value_text(share)} of the region")
    if counts["infeasible"]:
        print(f"  infeasible: {value_text(result['fractions']['infeasible'])} of the region")
    print(f"  undecided fraction: {value_text(result['undecided_fraction'])} "
          f"(exact: {result['undecided_fraction']}); resolution: "
          f"{decimal_text(result['resolution'])}")
    if result["resolution_reached"]:
        print("  The resolution is reached.")
    else:
        print(f"  The resolution is NOT reached: {stopped}.")
    print()
    for c in result["cells"]:
        box = ", ".join(f"{n} in [{decimal_text(lo)}, {decimal_text(hi)}]"
                        for n, (lo, hi) in c.box.items())
        if c.status == "decided":
            what = f"decided, best = {', '.join(c.best)}"
        elif c.reason:
            what = f"{c.status} ({c.reason})"
        else:
            what = c.status
        print(f"  {box}: {what}")
    return status


def _cmd_model_check(args: argparse.Namespace) -> int:
    from .stochastic import decimal_text
    from .stochastic_eval import (
        ESTIMATE_NOTE, NOTE, EvaluationError, check_properties, exact_text,
        parse_number, regime_map_estimate, value_text,
    )

    # Exit status: 0 when every threshold property evaluated is satisfied
    # (queries do not count), 1 when at least one is violated, 2 when
    # nothing could be evaluated (usage error, or no interpreted model).
    # With --certify: 0 when the certified map reaches its resolution, 1
    # when it does not.
    model = _load_stochastic_model(args.file)
    if model is None:
        return 2
    certified = None
    try:
        if args.certify and args.regime_map is None:
            raise EvaluationError("--certify needs --regime-map ID")
        if args.certify and args.grid is not None:
            raise EvaluationError(
                "--grid asks for estimates at sample points and --certify for a "
                "certified regime map; give one of them")
        if not args.certify and (args.max_depth is not None or args.max_cells is not None):
            raise EvaluationError("--max-depth and --max-cells need --certify")
        if args.certify and args.regime is not None:
            raise EvaluationError("--regime cannot be combined with --certify: the "
                                  "query lists the regimes it compares")
        overrides = {}
        for item in args.assignments:
            name, sep, value = item.partition("=")
            if not sep:
                raise EvaluationError(f"--set expects p=v, not '{item}'")
            overrides[name.strip()] = parse_number(value)
        if args.certify:
            from .stochastic_regions import (
                DEFAULT_MAX_CELLS, DEFAULT_MAX_DEPTH, certified_regime_map,
            )
            certified = certified_regime_map(
                model, args.regime_map, overrides,
                DEFAULT_MAX_DEPTH if args.max_depth is None else args.max_depth,
                DEFAULT_MAX_CELLS if args.max_cells is None else args.max_cells)
            estimate = None
            designs = []
        elif args.regime_map is not None:
            estimate = regime_map_estimate(
                model, args.regime_map, 4 if args.grid is None else args.grid, overrides)
            designs = []
        else:
            estimate = None
            designs = check_properties(model, args.regime, overrides)
    except EvaluationError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    status = 1 if any(p["result"] == "violated"
                      for d in designs for p in d["properties"]) else 0
    if certified is not None:
        return _print_certified_map(args, model, certified)

    if args.format == "json":
        import json
        output = {"file": str(args.file), "sos": model.sos_name, "evaluator": NOTE}
        if estimate is None:
            output["designs"] = [
                {
                    "design": d["design"],
                    "parameters": {k: decimal_text(v) for k, v in d["parameters"].items()},
                    "reachable_states": d["states"],
                    "properties": [
                        {"id": p["id"], "expr": p["expr"], "value": exact_text(p["value"]),
                         "decimal": value_text(p["value"]), "result": p["result"]}
                        for p in d["properties"]
                    ],
                }
                for d in designs
            ]
        else:
            output["regime_map_estimate"] = {
                **{k: estimate[k] for k in ("id", "note", "direction", "objective",
                                            "constraint", "open", "regimes", "grid")},
                "points": [
                    {
                        "point": {k: decimal_text(v) for k, v in pt["point"].items()},
                        "regimes": {
                            r: {"value": exact_text(x["value"]),
                                "decimal": value_text(x["value"]),
                                "constraint": x["constraint"], "sat": x["sat"]}
                            for r, x in pt["regimes"].items()
                        },
                        "best_at_point": pt["best"],
                    }
                    for pt in estimate["points"]
                ],
            }
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return status

    print(f"Model check: {args.file}")
    print(f"  SoS: {model.sos_name}")
    print(f"  Evaluator: {NOTE}")
    print()
    if estimate is None:
        for d in designs:
            params = ", ".join(f"{k}={decimal_text(v)}" for k, v in d["parameters"].items())
            print(f"--- Design {d['design']} ({params}; "
                  f"{d['states']} reachable state(s)) ---")
            for p in d["properties"]:
                result = f"  {p['result']}" if p["result"] else "  (query)"
                print(f"  {p['id']}: {p['expr']}")
                print(f"    value = {value_text(p['value'])}  (exact: "
                      f"{exact_text(p['value'])}){result}")
            if not d["properties"]:
                print("  (no property concerns this design)")
            print()
        return status

    print(f"--- Regime map query '{estimate['id']}': {ESTIMATE_NOTE} ---")
    print(f"  objective: {estimate['direction']} {estimate['objective']}")
    if estimate["constraint"]:
        print(f"  constraint: {estimate['constraint']}")
    print(f"  Each line is the exact evaluation at one sample point (grid of "
          f"{estimate['grid']} interval(s) per open parameter). No cell is decided.")
    for pt in estimate["points"]:
        at = ", ".join(f"{k}={decimal_text(v)}" for k, v in pt["point"].items())
        values = ", ".join(
            f"{r}={value_text(x['value'])}"
            + ("" if x["constraint"] else " (violates the constraint)")
            for r, x in pt["regimes"].items()
        )
        best = ", ".join(pt["best"]) if pt["best"] else "none (no regime satisfies the constraint)"
        print(f"  {at}: {values}; best at this point: {best}")
    return 0


def _cmd_ai(args: argparse.Namespace) -> int:
    from .ai import generate_cadl, ClientConfig

    # Get description
    description = args.description
    if args.input_file:
        if not args.input_file.exists():
            print(f"Error: File not found: {args.input_file}", file=sys.stderr)
            return 1
        description = args.input_file.read_text(encoding="utf-8")
    if not description:
        print("Error: Provide a description as argument or via -f <file>", file=sys.stderr)
        return 1

    # Configure client
    config = ClientConfig()
    if args.model:
        config.model = args.model

    # Generate
    print("Generating CADL from description...", file=sys.stderr)
    try:
        result = generate_cadl(description, config=config, verify=args.verify)
    except ImportError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Generation error: {e}", file=sys.stderr)
        return 1

    if result.errors:
        print("Warning: Generated CADL has validation issues:", file=sys.stderr)
        for err in result.errors:
            print(f"  {err}", file=sys.stderr)

    if result.retried:
        print("(retried after initial validation failure)", file=sys.stderr)

    # Output
    if args.output_file:
        args.output_file.write_text(result.cadl_source, encoding="utf-8")
        print(f"Saved to: {args.output_file}", file=sys.stderr)
    else:
        print(result.cadl_source)

    if result.sos:
        print(f"  SoS: {result.sos.name}", file=sys.stderr)
        print(f"  Actors: {len(result.sos.actors)}", file=sys.stderr)
        print(f"  Contracts: {len(result.sos.contracts)}", file=sys.stderr)
        print(f"  Protocols: {len(result.sos.protocols)}", file=sys.stderr)

    return 0 if not result.errors else 1


if __name__ == "__main__":
    sys.exit(main())
