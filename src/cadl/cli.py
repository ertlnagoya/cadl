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

    # codegen command
    codegen_cmd = subparsers.add_parser("codegen", help="Generate Python runtime code from a CADL file")
    codegen_cmd.add_argument("file", type=Path, help="CADL file to generate from")
    codegen_cmd.add_argument("--output", "-o", type=Path, default=Path("generated"),
                             help="Output directory (default: generated/)")

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
                        help="Override LLM model (default: claude-sonnet-4-20250514)")

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

    print(f"Verifying: {path}")
    print(f"  SoS: {sos.name}")
    print()
    has_failures = False

    # Type check
    print("--- Type Check ---")
    tc_result = type_check(sos)
    for w in tc_result.warnings:
        print(f"  {w}")
    for e in tc_result.errors:
        print(f"  {e}")
    if tc_result.ok:
        print("  [PASS] Type check passed")
    else:
        print(f"  [FAIL] {len(tc_result.errors)} error(s)")
        has_failures = True
    print()

    # SMT Verification
    print("--- SMT Verification ---")
    v_results = verify(sos)
    for r in v_results:
        print(f"  {r}")
        if r.status == "failed":
            has_failures = True
    if not v_results:
        print("  (no contracts to verify)")
    print()

    # Deadlock Detection
    print("--- Deadlock Detection ---")
    d_results = detect_deadlocks(sos)
    for r in d_results:
        print(f"  {r}")
        if r.status == "failed":
            has_failures = True
    if not d_results:
        print("  (no protocols to analyze)")
    print()

    # Summary
    total_checks = len(v_results) + len(d_results) + 1  # +1 for type check
    passed = sum(1 for r in v_results if r.status == "passed") + \
             sum(1 for r in d_results if r.status == "passed") + \
             (1 if tc_result.ok else 0)
    failed = total_checks - passed

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
    if not tc_result.ok:
        for e in tc_result.errors:
            print(f"  {e}", file=sys.stderr)
        print(f"Type check failed: cannot generate code", file=sys.stderr)
        return 1

    # Generate
    output_dir = args.output
    try:
        generate(sos, output_dir)
    except Exception as e:
        print(f"Code generation error: {e}", file=sys.stderr)
        return 1

    print(f"Code generated: {output_dir}/")
    print(f"  SoS: {sos.name}")
    print(f"  Actors: {len(sos.actors)}")
    print(f"  Contracts: {len(sos.contracts)}")
    print(f"  Protocols: {len(sos.protocols)}")
    print(f"  Metrics: {len(sos.metrics)}")
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
