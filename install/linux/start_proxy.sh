#!/usr/bin/env bash
# Wrapper for the systemd user service.
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-$HOME/ollama-proxy-qwen}"
PYTHON="${QWEN_PROXY_PYTHON:-$INSTALL_DIR/.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON=python3

cd "$INSTALL_DIR"
exec "$PYTHON" qwen_coder_proxy.py
