#!/usr/bin/env bash
#
# REAgraph demo: persistent memory + byte-agnostic structural diff.
#
# Runs three steps against the sample findings in ./sample_findings:
#   1. add   — fold a REA finding into a fresh persistent graph (/tmp)
#   2. stats — show what the accumulated memory now knows
#   3. diff  — structurally compare v1 vs v2 of the same program
#
# Re-runnable: it wipes its own /tmp graph at the start.

set -euo pipefail

# Resolve paths relative to this script, so it runs from anywhere.
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
FINDINGS="$HERE/sample_findings"
GRAPH="/tmp/reagraph_demo.json"

# Prefer the project venv's reagraph; fall back to the module.
if [ -x "$REPO/.venv/bin/reagraph" ]; then
  REAGRAPH="$REPO/.venv/bin/reagraph"
else
  REAGRAPH="python -m reagraph.cli"
fi

echo "== using: $REAGRAPH"
echo "== graph: $GRAPH"
echo

# Start from a clean slate so the demo is deterministic and re-runnable.
rm -f "$GRAPH"

echo "---------------------------------------------------------------"
echo "STEP 1  reagraph add  ->  build persistent memory from a finding"
echo "  A reverse-engineering finding (login.jag.json) is folded into a"
echo "  durable graph. Findings stop evaporating; memory compounds."
echo "---------------------------------------------------------------"
$REAGRAPH add "$FINDINGS/login.jag.json" --graph "$GRAPH"
echo

echo "---------------------------------------------------------------"
echo "STEP 2  reagraph stats  ->  what the accumulated graph knows"
echo "  Coverage of the persisted graph: node/edge counts and the"
echo "  observed(EXTRACTED) vs inferred(INFERRED) confidence split."
echo "---------------------------------------------------------------"
$REAGRAPH stats --graph "$GRAPH"
echo

echo "---------------------------------------------------------------"
echo "STEP 3  reagraph diff v1 v2  ->  structural, byte-agnostic diff"
echo "  v2 is the SAME program with every symbol relocated, plus ONE"
echo "  added exfil edge (session_create -> analytics.evil.example)."
echo "  A byte-offset diff would cry 'everything changed'. REAgraph"
echo "  reads it correctly: mostly unchanged, one node added, one edge"
echo "  added (session_create rewired)."
echo "---------------------------------------------------------------"
$REAGRAPH diff "$FINDINGS/login.jag.json" "$FINDINGS/login_v2.jag.json"
echo

echo "== done. persistent graph left at: $GRAPH"
