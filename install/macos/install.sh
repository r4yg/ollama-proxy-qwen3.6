#!/usr/bin/env bash
# install.sh — macOS installer for qwen3.6 + qwen-coder-proxy
#
# Steps:
#   1. Verify prerequisites (ollama, python3)
#   2. Pull qwen3.6:35b-a3b
#   3. Build local-coder:35b-a3b from the Modelfile
#   4. Install Python deps in a venv
#   5. Copy proxy + scripts to ~/ollama-proxy-qwen
#   6. Suggest Ollama env vars for the user (set in ~/.zshrc or LaunchAgent)
#   7. Install launchd agent that auto-starts the proxy at login

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-$HOME/ollama-proxy-qwen}"
PROXY_PORT="${QWEN_PROXY_PORT:-18000}"
SKIP_MODEL_PULL="${SKIP_MODEL_PULL:-0}"
SKIP_MODEL_BUILD="${SKIP_MODEL_BUILD:-0}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
PLIST_LABEL="com.user.qwen-coder-proxy"
PLIST_FILE="$LAUNCH_AGENTS/${PLIST_LABEL}.plist"

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
    echo "python3 not found. Install via 'brew install python' or python.org" >&2
    exit 1
}
green "python3: $(command -v python3)"

cyan "Creating install dir: $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp "$REPO_ROOT/qwen_coder_proxy.py" "$INSTALL_DIR/"
cp "$REPO_ROOT/local-coder.Modelfile" "$INSTALL_DIR/"
cp "$REPO_ROOT/requirements.txt" "$INSTALL_DIR/"
cp "$REPO_ROOT/install/macos/start_proxy.sh" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/start_proxy.sh"
green "files copied"

cyan "Setting up Python venv with deps"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
# Make start_proxy.sh use the venv python
sed -i.bak 's|python3|'"$INSTALL_DIR"'/.venv/bin/python|' "$INSTALL_DIR/start_proxy.sh"
rm -f "$INSTALL_DIR/start_proxy.sh.bak"
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

cyan "Installing launchd agent"
mkdir -p "$LAUNCH_AGENTS"
sed "s|__INSTALL_DIR__|$INSTALL_DIR|g; s|18000|$PROXY_PORT|g" \
    "$REPO_ROOT/install/macos/com.user.qwen-coder-proxy.plist" \
    > "$PLIST_FILE"

# Reload the agent
launchctl unload "$PLIST_FILE" 2>/dev/null || true
launchctl load -w "$PLIST_FILE"
green "launchd agent loaded: $PLIST_FILE"

cyan "Recommended Ollama env vars (add to ~/.zshrc or use launchctl setenv)"
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
    yellow "proxy did not bind — check $INSTALL_DIR/proxy.log"
fi

cat <<EOF

=== install complete ===
  install dir : $INSTALL_DIR
  proxy port  : $PROXY_PORT
  log file    : $INSTALL_DIR/proxy.log
  launchd     : launchctl list | grep $PLIST_LABEL

Next steps:
  1. Set the Ollama env vars (above) and restart Ollama
  2. Point opencode at http://YOUR_IP:$PROXY_PORT/v1
     (see examples/opencode-config.json)
  3. Use model id  local-coder:35b-a3b

Manage the agent:
  launchctl unload $PLIST_FILE   # stop
  launchctl load   $PLIST_FILE   # start
EOF
