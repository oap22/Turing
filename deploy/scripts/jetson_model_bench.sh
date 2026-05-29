#!/usr/bin/env bash
# jetson_model_bench.sh — measure fit + throughput of the pinned worker model on
# a real Jetson Orin Nano Super (ADR 0009, issue #259).
#
# Records the numbers the deploy notes need; it does NOT estimate them. Run on
# one node, in MAXN SUPER mode, then paste the output into deploy/jetson-worker.md.
#
# Usage: deploy/scripts/jetson_model_bench.sh [model-tag] [prompt]
set -euo pipefail

MODEL="${1:-qwen2.5:7b-instruct-q4_K_M}"
PROMPT="${2:-Explain LoRA fine-tuning and why low-rank adapters save memory. Be concise.}"

echo "== Jetson worker model benchmark =="
echo "model:  $MODEL"
echo

# Power mode (informational — set MAXN SUPER with: sudo nvpmodel -m 2 && sudo jetson_clocks)
if command -v nvpmodel >/dev/null 2>&1; then
  echo "-- nvpmodel --"; sudo nvpmodel -q || true; echo
fi

# Sample memory before/after so headroom within 8 GB is visible.
if command -v tegrastats >/dev/null 2>&1; then
  echo "-- tegrastats (5s sample; watch the RAM x/8192MB field during the run) --"
  timeout 5 tegrastats || true
  echo
fi

echo "-- pulling model (no-op if present) --"
ollama pull "$MODEL"

echo
echo "-- inference (eval rate = tokens/s; load duration = fit/load cost) --"
# --verbose prints total/load/eval durations and the eval rate (tokens/s).
ollama run --verbose "$MODEL" "$PROMPT"

echo
echo "Record from above into deploy/jetson-worker.md:"
echo "  * eval rate (tokens/s)"
echo "  * peak RAM (x/8192MB) from tegrastats during the run -> headroom"
echo "  * that it loaded + answered without OOM"
