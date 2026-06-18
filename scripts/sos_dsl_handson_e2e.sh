#!/usr/bin/env bash
# sos_dsl_handson_e2e.sh
#
# End-to-end SoS-DSL handson pipeline for one CADL source file:
#
#   .cadl  ─►  parse + type-check     (cadl check)
#          ─►  Sim-IR JSON            (cadl sim-ir   --format json)
#          ─►  Unity C# tree          (cadl codegen  --target unity-csharp)
#          ─►  drop into Unity Assets/Scripts/SoSDsl/
#
# Each step prints a one-line summary so the student can see what
# changes between stages.
#
# Usage:
#   ./scripts/sos_dsl_handson_e2e.sh                                  # default example
#   ./scripts/sos_dsl_handson_e2e.sh examples/sos_dsl_robot_delivery.cadl
#   ./scripts/sos_dsl_handson_e2e.sh \
#       examples/my_delivery.cadl \
#       --unity ../raspimouse-swarm-simulator/unity \
#       --output output/my_delivery
#
# Flags:
#   --unity <dir>    Unity project root to drop generated C# into.
#                    Default: ../raspimouse-swarm-simulator/unity
#                    (relative to the cadl_repo root). Pass "" to skip
#                    Unity drop.
#   --output <dir>   Where to put intermediate artefacts (IR JSON +
#                    raw C# tree). Default: output/sos_dsl_handson.
#   -h, --help       Print this and exit.
#
# Exit codes:
#   0   pipeline succeeded
#   2   parse / type-check failed
#   3   IR emission failed
#   4   codegen failed
#   5   Unity drop failed (e.g. directory missing)

set -euo pipefail

usage() {
    sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
}

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# Prefer a working venv if present; fall back to the in-repo source.
VENV_CADL="${REPO_DIR}/.venv/bin/cadl"
VENV_PY="${REPO_DIR}/.venv/bin/python3"
if [[ -x "$VENV_CADL" ]] && "$VENV_PY" -c "import cadl" >/dev/null 2>&1; then
    CADL="$VENV_CADL"
    PYTHON="$VENV_PY"
else
    CADL="python3 -m cadl.cli"
    PYTHON="python3"
    export PYTHONPATH="${REPO_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"
fi

INPUT="${REPO_DIR}/examples/sos_dsl_robot_delivery.cadl"
OUTPUT_DIR="${REPO_DIR}/output/sos_dsl_handson"
UNITY_DIR="${REPO_DIR}/../raspimouse-swarm-simulator/unity"

# Parse args
positional=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --unity)   UNITY_DIR="$2"; shift 2 ;;
        --output)  OUTPUT_DIR="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        --) shift; while [[ $# -gt 0 ]]; do positional+=("$1"); shift; done ;;
        -*) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
        *)  positional+=("$1"); shift ;;
    esac
done
if [[ ${#positional[@]} -ge 1 ]]; then
    INPUT="${positional[0]}"
fi

if [[ ! -f "$INPUT" ]]; then
    echo "ERROR: input CADL not found: $INPUT" >&2
    exit 2
fi
mkdir -p "$OUTPUT_DIR"

label() {
    printf "\n\033[1;36m== %s ==\033[0m\n" "$1"
}

label "INPUT"
echo "  source : $INPUT"
echo "  size   : $(wc -l < "$INPUT") lines"

# ── Step 1: parse + type-check ─────────────────────────────────────
label "Step 1/4 — parse + type-check (cadl check)"
if ! $CADL check "$INPUT" >/dev/null; then
    echo "  parse / type-check FAILED" >&2
    exit 2
fi
echo "  OK"

# ── Step 2: lower to Sim-IR JSON ───────────────────────────────────
label "Step 2/4 — emit Sim-IR JSON (cadl sim-ir)"
IR_PATH="$OUTPUT_DIR/$(basename "$INPUT" .cadl).ir.json"
if ! $CADL sim-ir "$INPUT" --format json > "$IR_PATH"; then
    echo "  IR emission FAILED" >&2
    exit 3
fi
echo "  $IR_PATH"
echo "  contracts : $($PYTHON -c "import json,sys;d=json.load(open('$IR_PATH'));print(len(d['institution']['contracts']))")"
echo "  states    : $($PYTHON -c "import json,sys;d=json.load(open('$IR_PATH'));c=d['institution']['contracts'][0];lc=c.get('lifecycle');print(len(lc['states']) if lc else 0)")"
echo "  monitors  : $($PYTHON -c "import json,sys;d=json.load(open('$IR_PATH'));c=d['institution']['contracts'][0];print(len(c.get('monitors',[])))")"

# ── Step 3: codegen Unity C# ──────────────────────────────────────
label "Step 3/4 — codegen Unity C# (cadl codegen --target unity-csharp)"
CSHARP_DIR="$OUTPUT_DIR/unity-csharp"
rm -rf "$CSHARP_DIR" 2>/dev/null || true
if ! $CADL codegen "$INPUT" --target unity-csharp --output "$CSHARP_DIR" >/dev/null; then
    echo "  codegen FAILED" >&2
    exit 4
fi
n_runtime=$(find "$CSHARP_DIR/Runtime" -name "*.cs" 2>/dev/null | wc -l | tr -d ' ')
n_generated=$(find "$CSHARP_DIR/Generated" -name "*.cs" 2>/dev/null | wc -l | tr -d ' ')
total_lines=$(find "$CSHARP_DIR" -name "*.cs" -exec cat {} + 2>/dev/null | wc -l | tr -d ' ')
echo "  output    : $CSHARP_DIR"
echo "  runtime   : $n_runtime files"
echo "  generated : $n_generated files"
echo "  total lines : $total_lines"

# ── Step 4: drop into Unity Assets/Scripts/SoSDsl/ ────────────────
if [[ -z "$UNITY_DIR" ]]; then
    label "Step 4/4 — Unity drop (skipped, --unity \"\")"
    echo "  no Unity directory configured; pipeline ends after codegen."
    exit 0
fi

label "Step 4/4 — drop into $UNITY_DIR/Assets/Scripts/SoSDsl/"
if [[ ! -d "$UNITY_DIR/Assets" ]]; then
    echo "  Unity project not found at: $UNITY_DIR" >&2
    echo "  pass --unity <other> to point elsewhere, or --unity \"\" to skip" >&2
    exit 5
fi
DEST="$UNITY_DIR/Assets/Scripts/SoSDsl"
mkdir -p "$DEST"
# Replace Runtime/ and Generated/, but preserve any hand-written
# files under Demo/ (DeliveryContractDemo.cs / ContractRuntimeHost.cs /
# PilotContractBridge.cs etc.) — these are project-owned.
#
# We use Python's shutil for the copy because the naive
#   rm -rf X && cp -r SRC X
# pattern misbehaves on filesystems where unlink is restricted (e.g.
# the cowork sandbox): the rm silently fails, X still exists, then
# `cp -r SRC X` creates X/SRC nested instead of overwriting X. The
# Python version overwrites file-by-file via O_TRUNC, which works on
# both restricted and normal filesystems.
"$PYTHON" - <<PY
import shutil, sys
from pathlib import Path
src = Path("$CSHARP_DIR")
dst = Path("$DEST")
for sub in ("Runtime", "Generated"):
    sd = src / sub
    dd = dst / sub
    dd.mkdir(parents=True, exist_ok=True)
    # Self-heal: a prior run with `cp -r SRC dd` (or an older version of
    # this script) on a restricted-unlink filesystem can leave a nested
    # dd/<sub> (e.g. Generated/Generated, Runtime/Runtime). Those nested
    # copies duplicate every type and make Unity fail to compile
    # (CS0101/CS0111). Remove any such nested directory before copying.
    nested = dd / sub
    if nested.is_dir():
        shutil.rmtree(nested, ignore_errors=True)
    # Remove stale .cs files that the new generation no longer emits.
    keep = {p.name for p in sd.iterdir() if p.is_file()}
    for old in dd.glob("*.cs"):
        if old.name not in keep:
            try: old.unlink()
            except OSError: pass
    # Overwrite files one by one.
    for f in sd.iterdir():
        if f.is_file():
            shutil.copyfile(f, dd / f.name)
shutil.copyfile(src / "README.md", dst / "README.md")
PY
echo "  installed: $DEST/Runtime, $DEST/Generated"
echo "  preserved: $DEST/Demo (if it existed)"

label "DONE"
echo "Next steps for the student:"
echo "  1. Open the Unity project at $UNITY_DIR in Unity 2022.3.x"
echo "  2. Open Assets/Scenes/C-SoS.unity"
echo "  3. Add a ContractRuntimeHost GameObject (Demo/ContractRuntimeHost)"
echo "  4. Attach PilotContractBridge to each robot that has Pilot_CSoS"
echo "  5. Press Play and watch the Console for [lifecycle ...] / [violation ...]"
echo ""
echo "See sos-dsl-unity-runbook.md for the full walkthrough."
