# Changelog

All notable changes to the CADL language and toolchain are documented in
this file. The format follows [Keep a Changelog](https://keepachangelog.com/)
and the project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- The event-loop tests used `asyncio.get_event_loop()`, which raises on
  Python 3.14 when no loop is running. They use `asyncio.run()` now, and
  Python 3.14 is part of the CI matrix. The library itself needed no change.

## [0.3.6] — 2026-10-09

### Changed
- Generated Solidity files carry `SPDX-License-Identifier: Apache-2.0`
  (was `MIT`), matching the license of the toolchain. Edit the line if you
  release generated contracts under another license.
- The default model of `cadl ai` is `claude-sonnet-5-5` (was
  `claude-sonnet-4-20250514`). Set `CADL_LLM_MODEL` or pass `--model` to
  use another one.
- The CI and publish workflows declare `permissions: contents: read`; the
  publish job keeps `id-token: write` for Trusted Publishing.

## [0.3.5] — 2026-10-09

### Fixed
- `cadl codegen --target unity-csharp` emitted the `case` lines of
  `IsTerminal` in an order that changed from run to run (the terminal states
  were iterated as a set), so regenerating from an unchanged file produced a
  different `*Contract.cs` each time. They now follow the order in which
  `terminal:` declares them. A test regenerates every code target under
  several hash seeds and requires identical output.

## [0.3.4] — 2026-10-09

Aligns the implementation with the specification (Appendix A / E) where a
cross-check found them apart.

### Fixed
- `cadl check` reported a predicate that is a single name (`"system_ready"`)
  as an undefined actor, and did not look for undeclared actors inside
  comparisons or `AND` / `OR` / `NOT`. A name is now treated as an actor,
  and must be declared, when it is indexed (`ROBOT[i]`) or is the object of a
  member access (`GHOST.x > 5`); a bare name is taken as a state variable.
  Variables bound by a quantifier or a comprehension are exempt.
- A quantifier could only start an expression. It may now follow `AND`,
  `OR` and `NOT` as in spec A.10 (`a AND for all x in X: p(x)`), and scopes
  over everything to its right.
- `in`, `exists` and `IN` were accepted as identifiers although the spec
  reserves them.
- A `verification:` entry with `method: smt` passed whatever it contained.
  It now fails when `target:` does not name a declared contract, protocol,
  regime or transition, or when `expr:` is unsatisfiable. `expr` is still
  not proved against the model; the message says so.
- `scripts/sos_dsl_handson_e2e.sh` pointed to a runbook file that does not
  exist; it now points to the hands-on textbook.
- The NL-to-CADL prompt listed autonomy levels `none` and `full`, which the
  language does not have.

### Changed
- IEC 62853 summary: `lambda` is labelled "incentive strength" and described
  on the scale the specification defines (0 = directive-based, 1 = market
  mechanism) instead of as "incentive alignment" / "misaligned incentives".
- README: lists `unity-csharp` as the fourth code generation target and
  completes the project structure listing.

## [0.3.3] — 2026-10-09

### Security
- `cadl codegen` for the `python`, `solidity` and `opa` targets wrote ids and
  text from the `.cadl` file into generated source verbatim. A crafted
  string could add code to the generated module, and a contract id such as
  `../../x` wrote a file outside the output directory. Code generation now
  checks the definition first and refuses ids that are not identifiers and
  text containing quotes, backslashes, backticks, `*/` or control
  characters (`cadl.codegen.safety`). The Unity C# target already sanitised
  names and escaped literals.

### Changed
- License changed from MIT to Apache License 2.0 (`LICENSE`, `NOTICE`).
  Releases up to and including 0.3.2 remain available under the MIT License.
- `cadl iec62853` is described as an IEC 62853-oriented dependability
  summary instead of a "compliance report". The indicator names are defined
  by CADL, not by the standard, and the report does not assess conformance;
  the text output and a new `disclaimer` key in the JSON say so. JSON keys
  are otherwise unchanged.
- The hands-on script and documentation point to the public
  `cadl-raspimouse-simulator` repository and to Unity 6 (6000.2);
  `sos_dsl_handson_e2e.sh` now defaults to `../cadl-raspimouse-simulator/unity`.
- `beta` is documented consistently as decision **centralization**
  (0 = fully distributed, 1 = centralized), which is how the IEC 62853
  report, the simulator IR and most examples already used it. The README
  definition and the NL-to-CADL prompt said the opposite. Values written
  under the old wording were mirrored (`1 - beta`):
  `examples/robot_delivery.cadl` 0.2 → 0.8,
  `examples/household_chores.cadl` 0.3 → 0.7, and the README / prompt
  samples.
- README: dropped the links to the instructor course-design page (removed
  from the spec site) and marked `raspimouse-swarm-simulator` as not
  publicly available.

### Fixed
- Parser ignored `method:`, `expr:` and `bound:` in `verification:` entries,
  so `method: model_check` (or `simulation` / `proof`) written in a `.cadl`
  file was treated as the default `smt` and reported as passed. The fields
  are now read, and non-SMT methods surface as `not_supported` from
  `cadl verify`, as the 0.3.0 entry describes.
- `scripts/sos_dsl_handson_e2e.sh` ran a command quoted in one of its
  comments on every invocation, leaving a stray `dd/` copy of `src/` in the
  repository on case-insensitive file systems.

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

[Unreleased]: https://github.com/ertlnagoya/cadl/compare/v0.3.6...HEAD
[0.3.6]: https://github.com/ertlnagoya/cadl/compare/v0.3.5...v0.3.6
[0.3.5]: https://github.com/ertlnagoya/cadl/compare/v0.3.4...v0.3.5
[0.3.4]: https://github.com/ertlnagoya/cadl/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/ertlnagoya/cadl/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/ertlnagoya/cadl/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/ertlnagoya/cadl/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/ertlnagoya/cadl/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/ertlnagoya/cadl/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/ertlnagoya/cadl/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/ertlnagoya/cadl/releases/tag/v0.2.0
