# Changelog

All notable changes to the CADL language and toolchain are documented in
this file. The format follows [Keep a Changelog](https://keepachangelog.com/)
and the project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/ertlnagoya/cadl/compare/v0.3.2...HEAD
[0.3.2]: https://github.com/ertlnagoya/cadl/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/ertlnagoya/cadl/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/ertlnagoya/cadl/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/ertlnagoya/cadl/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/ertlnagoya/cadl/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/ertlnagoya/cadl/releases/tag/v0.2.0
