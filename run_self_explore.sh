#!/usr/bin/env bash
# Launch Self-Explorer: Gemma4 brain + unified server on port 1010
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LLAMA_SERVER="$HOME/Code/llama.cpp/build/bin/llama-server"
MODEL="$HOME/google_gemma-4-E4B-it-Q4_K_M.gguf"

echo "=== SimpleAgentOS Self-Explorer ==="
echo "  BRAIN:  llama-server on :8080 (CPU mode)"
echo "  APP:    self-explorer on :1010"
echo ""
echo "  Open: http://localhost:1010"
echo ""

npx concurrently \
  --names "BRAIN,APP" \
  --prefix-colors "magenta,cyan" \
  "GGML_METAL=0 $LLAMA_SERVER -m $MODEL --port 8080 -ngl 0 -c 2048 -np 1 -b 256 --no-warmup" \
  "cd $SCRIPT_DIR && python3 self_explore_server.py"
