# Changelog

All notable changes to the CADL language and toolchain are documented in
this file. The format follows [Keep a Changelog](https://keepachangelog.com/)
and the project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `LICENSE` (MIT) and full packaging metadata in `pyproject.toml` (authors,
  keywords, classifiers, project URLs, hatch wheel/sdist targets).
- GitHub Actions workflow running `pytest` on Python 3.9–3.12 and building
  the sdist + wheel on every push.
- `CHANGELOG.md` (this file).
- `VerificationSpec.method` / `expr` / `bound` fields and
  `cadl.verifier.dispatch_spec()` implementing explicit `not_supported`
  results for `model_check` / `simulation` / `proof` methods per
  Appendix A §A.8 (previously these were silently skipped).
- Optional `MotivationBlock` / `AgentMotivationBlock` /
  `GovernanceMotivationBlock` AST nodes and `SoSDefinition.motivation`
  field for CADL Appendix C (v0.1-ext) motivation extension parity with
  `cadl-explorer`.
- Integration tests `tests/test_method_dispatch.py` (11 new cases).

### Changed
- Fixed attribution in `README.md` / `README_ja.md` from "Matsubara
  Laboratory" / "松原研究室" to "ERTL".
- `CodegenSpec.target` docstring updated to enumerate the canonical
  target catalog (`unity` / `ros2` / `python` / `solidity` / `opa` /
  user identifier) to match Appendix D of the spec.

## [0.1.0] — 2026-03-17

Initial draft release of the CADL compiler.

### Added
- Lark grammar (`src/cadl/grammar.lark`) and parser producing an AST for
  the three-layer CADL syntax (Institution / Protocol / Algorithm).
- Type checker and deadlock analyzer (`type_checker.py`, `deadlock.py`).
- Verifier integrating Z3 for safety / liveness / fairness / invariant
  properties declared in `verification:` blocks.
- Code generators:
  - Unity runtime config
  - Solidity smart contracts (`codegen/solidity/`)
  - OPA / Rego policies (`codegen/opa/`)
  - Actor / contract / metric / protocol / transition scaffolds.
- IEC 62853 dependability-regime mapping (`iec62853.py`).
- AI-assisted natural-language → CADL translation (`ai/`), gated behind
  the optional `ai` extra (requires `anthropic`).
- CLI entry point `cadl` with subcommands for parsing, verification and
  code generation.
- Example CADL files under `examples/`.
- Pytest test suite covering parser, type checker, verifier, and codegen.

[Unreleased]: https://github.com/ertlnagoya/cadl/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ertlnagoya/cadl/releases/tag/v0.1.0
