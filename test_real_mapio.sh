#!/usr/bin/env bash
# Real MapIO Graph Tool Execution Evaluator (WSL Native Script)
# Usage: ./test_real_mapio.sh [SERVER_URL] [MODEL_NAME] [QUESTION]

SERVER_URL="${1:-http://localhost:11434/v1}"
MODEL_NAME="${2:-auto}"
QUESTION="${3:-what shops or restaurants are near 5th avenue?}"

PYTHON_BIN="/home/znasif/anaconda3/envs/braille/bin/python"
SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/run_real_camio_eval.py"

echo "============================================================"
echo " Real MapIO Graph Tool Execution Evaluator (WSL)"
echo " Server:   $SERVER_URL"
echo " Model:    $MODEL_NAME"
echo " Question: \"$QUESTION\""
echo "============================================================"
echo ""

"$PYTHON_BIN" "$SCRIPT_PATH" --server "$SERVER_URL" --model "$MODEL_NAME" --question "$QUESTION"
