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
                             choices=["python", "solidity", "opa", "unity-csharp"],
                             default="python",
                             help="Code generation target (default: python)")
    codegen_cmd.add_argument("--output", "-o", type=Path, default=Path("generated"),
                             help="Output directory (default: generated/)")

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
    from .codegen import generate

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
        print(json.dumps(_ir_to_dict(ir), indent=2, ensure_ascii=False))
    else:
        import yaml
        print(yaml.dump(_ir_to_dict(ir), default_flow_style=False,
                        sort_keys=False, allow_unicode=True))

    return 0


def _ir_to_dict(ir) -> dict:
    """Serialize SimIR to a plain dict for YAML/JSON output."""
    from dataclasses import asdict
    d = asdict(ir)
    # Rename lambda_ back to lambda for readability
    for c in d.get("institution", {}).get("contracts", []):
        gov = c.get("governance", {})
        if "lambda_" in gov:
            gov["lambda"] = gov.pop("lambda_")
    # The verbatim motivation block is an optional key: present only when
    # the source has a `motivation:` block.
    if d.get("motivation_block") is None:
        d.pop("motivation_block", None)
    return d


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
