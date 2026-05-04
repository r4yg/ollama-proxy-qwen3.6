#!/usr/bin/env bash
# Wrapper for the macOS launchd agent. Logs go to $LOG.
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-$HOME/ollama-proxy-qwen}"
LOG="$INSTALL_DIR/proxy.log"
PYTHON="${QWEN_PROXY_PYTHON:-python3}"

cd "$INSTALL_DIR"
echo "[$(date)] proxy starting" >> "$LOG"
exec "$PYTHON" qwen_coder_proxy.py >> "$LOG" 2>&1
