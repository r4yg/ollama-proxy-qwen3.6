#!/usr/bin/env bash
# install.sh — Linux installer for qwen3.6 + qwen-coder-proxy
#
# Installs a systemd USER service (no sudo required for the proxy itself).
# Steps:
#   1. Verify prerequisites (ollama, python3)
#   2. Pull qwen3.6:35b-a3b
#   3. Build local-coder:35b-a3b from the Modelfile
#   4. Install Python deps in a venv
#   5. Copy proxy + scripts to ~/ollama-proxy-qwen
#   6. Suggest Ollama env vars
#   7. Install systemd user unit and enable+start it

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-$HOME/ollama-proxy-qwen}"
PROXY_PORT="${QWEN_PROXY_PORT:-18000}"
SKIP_MODEL_PULL="${SKIP_MODEL_PULL:-0}"
SKIP_MODEL_BUILD="${SKIP_MODEL_BUILD:-0}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SYSTEMD_USER="$HOME/.config/systemd/user"
UNIT_NAME="qwen-coder-proxy.service"

cyan()  { printf '\033[36m==> %s\033[0m\n' "$*"; }
green() { printf '\033[32m    OK %s\033[0m\n' "$*"; }
yellow(){ printf '\033[33m    !! %s\033[0m\n' "$*"; }

cyan "Prerequisite check: ollama"
command -v ollama >/dev/null || {
    echo "ollama not found. Install from https://ollama.com/download" >&2
    exit 1
}
green "ollama: $(command -v ollama)"

cyan "Prerequisite check: python3"
command -v python3 >/dev/null || {
    echo "python3 not found. Install via your package manager." >&2
    exit 1
}
green "python3: $(command -v python3)"

cyan "Creating install dir: $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp "$REPO_ROOT/qwen_coder_proxy.py" "$INSTALL_DIR/"
cp "$REPO_ROOT/local-coder.Modelfile" "$INSTALL_DIR/"
cp "$REPO_ROOT/requirements.txt" "$INSTALL_DIR/"
cp "$REPO_ROOT/install/linux/start_proxy.sh" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/start_proxy.sh"
green "files copied"

cyan "Setting up Python venv with deps"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
green "venv ready: $INSTALL_DIR/.venv"

if [ "$SKIP_MODEL_PULL" != "1" ]; then
    cyan "Pulling qwen3.6:35b-a3b (~22 GB — this can take a while)"
    ollama pull qwen3.6:35b-a3b
    green "model pulled"
else
    yellow "skipping model pull (SKIP_MODEL_PULL=1)"
fi

if [ "$SKIP_MODEL_BUILD" != "1" ]; then
    cyan "Building local-coder:35b-a3b from Modelfile"
    ollama create local-coder:35b-a3b -f "$INSTALL_DIR/local-coder.Modelfile"
    green "local-coder:35b-a3b created"
else
    yellow "skipping model build (SKIP_MODEL_BUILD=1)"
fi

cyan "Installing systemd user unit"
mkdir -p "$SYSTEMD_USER"
sed "s|__INSTALL_DIR__|$INSTALL_DIR|g; s|18000|$PROXY_PORT|g" \
    "$REPO_ROOT/install/linux/qwen-coder-proxy.service" \
    > "$SYSTEMD_USER/$UNIT_NAME"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"
green "service enabled and started"

# Allow proxy to keep running after logout
loginctl enable-linger "$USER" 2>/dev/null || true

cyan "Recommended Ollama env vars (add to ~/.bashrc or /etc/environment)"
cat <<EOF
    export OLLAMA_KEEP_ALIVE=-1
    export OLLAMA_MAX_LOADED_MODELS=1
    export OLLAMA_NUM_PARALLEL=1
    export OLLAMA_HOST=0.0.0.0:11434
EOF
yellow "restart Ollama after setting these"

sleep 3
if curl -sf "http://localhost:$PROXY_PORT/v1/models" >/dev/null; then
    green "proxy listening on 0.0.0.0:$PROXY_PORT"
else
    yellow "proxy did not bind — check $INSTALL_DIR/proxy.log and 'systemctl --user status $UNIT_NAME'"
fi

cat <<EOF

=== install complete ===
  install dir : $INSTALL_DIR
  proxy port  : $PROXY_PORT
  log file    : $INSTALL_DIR/proxy.log

Next steps:
  1. Set the Ollama env vars (above) and restart Ollama
  2. Point opencode at http://YOUR_IP:$PROXY_PORT/v1
     (see examples/opencode-config.json)
  3. Use model id  local-coder:35b-a3b

Manage the service:
  systemctl --user status   $UNIT_NAME
  systemctl --user restart  $UNIT_NAME
  systemctl --user stop     $UNIT_NAME
  journalctl --user -u $UNIT_NAME -f
EOF
