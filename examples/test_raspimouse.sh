#!/bin/bash
# ============================================================
# test_raspimouse.sh
#
# raspimouse用.cadlファイルのパース・検証・Unity設定生成テスト
#
# 使い方:
#   cd ~/program/cadl_repo
#   pip install -e .   # 初回のみ
#   bash examples/test_raspimouse.sh
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "========================================"
echo "1. Parse test"
echo "========================================"
for f in raspimouse_d_sos.cadl raspimouse_c_sos.cadl raspimouse_mcp_sos.cadl; do
    echo "-- Parsing $f..."
    cadl parse "$SCRIPT_DIR/$f"
    echo "   OK"
done
echo ""

echo "========================================"
echo "2. Sim-validate test"
echo "========================================"
for f in raspimouse_d_sos.cadl raspimouse_c_sos.cadl raspimouse_mcp_sos.cadl; do
    echo "-- Validating $f..."
    cadl sim-validate "$SCRIPT_DIR/$f"
    echo "   OK"
done
echo ""

echo "========================================"
echo "3. Sim-IR output"
echo "========================================"
for f in raspimouse_d_sos.cadl raspimouse_c_sos.cadl raspimouse_mcp_sos.cadl; do
    echo "-- $f IR (JSON):"
    cadl sim-ir "$SCRIPT_DIR/$f" --format json | head -30
    echo "   ..."
    echo ""
done

echo "========================================"
echo "4. Unity config generation"
echo "========================================"
mkdir -p "$SCRIPT_DIR/../output"
for f in raspimouse_d_sos raspimouse_c_sos raspimouse_mcp_sos; do
    echo "-- Generating Unity config for $f..."
    cadl sim-gen "$SCRIPT_DIR/${f}.cadl" --target unity --output "$SCRIPT_DIR/../output/${f}_unity.json"
    echo "   -> output/${f}_unity.json"
done
echo ""

echo "========================================"
echo "5. Regime map"
echo "========================================"
for f in raspimouse_d_sos.cadl raspimouse_c_sos.cadl raspimouse_mcp_sos.cadl; do
    echo "-- $f regime transitions:"
    cadl regime-map "$SCRIPT_DIR/$f" --format text
    echo ""
done

echo "========================================"
echo "完了！生成されたファイル:"
echo "========================================"
ls -la "$SCRIPT_DIR/../output/"raspimouse_*
