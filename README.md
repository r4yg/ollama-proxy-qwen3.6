# ollama-proxy-qwen3.6

A drop-in proxy + Ollama Modelfile that makes **Qwen3.6-35B-A3B** work
correctly with OpenAI-API agentic clients (e.g.
[opencode](https://github.com/sst/opencode)) when served through Ollama.

```
┌──────────┐  OpenAI-compat  ┌────────────────┐  OpenAI-compat  ┌────────┐
│ opencode │ ───────────────▶│ qwen-coder-    │ ───────────────▶│ Ollama │
│ (Mac)    │   tool_calls    │ proxy (:18000) │  + transforms   │ :11434 │
└──────────┘ ◀───────────────└────────────────┘ ◀───────────────└────────┘
                                XML → tool_calls
                                <think> → reasoning_content
                                fill missing required args
                                schema-aware coercion
                                real-time SSE
```

## Why this exists

Qwen3.6-35B-A3B emits tool calls in **qwen3_coder XML** format:

```
<tool_call>
<function=NAME>
<parameter=KEY>VALUE</parameter>
</function>
</tool_call>
```

Ollama 0.21.x's tool parser only knows Hermes-style JSON, so the XML falls
through as plain assistant text. opencode (and other OpenAI-API clients) then
see plain text, not a `tool_calls` field, and the agent stalls with no action
taken. Multiple GitHub issues describe this exact symptom
(`anomalyco/opencode#24316`, `jundot/omlx#903`).

This repo fixes it without forking Ollama or running vLLM:

* **Ollama Modelfile (`local-coder.Modelfile`)** — overlays the official
  Qwen3.6 chat template (qwen3_coder XML, prompt-prefilled `<think>`) plus
  pinned context (60k), tool-use sampling, and several anti-stall rules
  baked in.
* **Python proxy (`qwen_coder_proxy.py`)** — sits between any OpenAI client
  and Ollama on a separate port:
  - Parses qwen3_coder XML on the wire and lifts it into OpenAI `tool_calls`
  - Coerces parameter values per-tool-schema (string params stay strings,
    array/object params get JSON-decoded)
  - Defensively fills missing required args (e.g. `description` for bash
    tools when the model omits it)
  - Splits `<think>...</think>` reasoning out of `content` into
    `reasoning_content` so opencode renders it as a collapsible block
  - Real-time streaming: forwards Ollama's SSE chunk-by-chunk and transforms
    on the fly

## Hardware / software prerequisites

| | |
|---|---|
| **GPU**            | ≥ 24 GB VRAM (tested on RTX 3090). Q4_K_M weights fit; the kv-cache for 60k context is q8_0 quantized. |
| **System RAM**     | 32 GB+ recommended (output layer is offloaded to CPU at 60k context) |
| **Disk**           | ~22 GB for the model |
| **Ollama**         | 0.21.x (any recent build) |
| **Python**         | 3.10+ |
| **OS**             | Windows 11 / macOS 13+ / Linux with systemd |

## Quick install

Clone this repo first:

```bash
git clone https://github.com/r4yg/ollama-proxy-qwen3.6.git
cd ollama-proxy-qwen3.6
```

### Windows (PowerShell, elevated)

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install\windows\install.ps1
```

### macOS

```bash
chmod +x install/macos/install.sh
./install/macos/install.sh
```

### Linux

```bash
chmod +x install/linux/install.sh
./install/linux/install.sh
```

Each installer:

1. Verifies `ollama` and `python3` are on PATH
2. Pulls `qwen3.6:35b-a3b` (~22 GB)
3. Builds `local-coder:35b-a3b` from `local-coder.Modelfile`
4. Installs Python deps (`fastapi`, `uvicorn`, `httpx`)
5. Copies the proxy + wrapper to `~/ollama-proxy-qwen` (or
   `%USERPROFILE%\ollama-proxy-qwen` on Windows)
6. Registers the service so it auto-starts on login:
   - **Windows** → Scheduled Task `qwen-coder-proxy`
   - **macOS** → launchd agent `~/Library/LaunchAgents/com.user.qwen-coder-proxy.plist`
   - **Linux** → systemd user unit `~/.config/systemd/user/qwen-coder-proxy.service`
7. Prints the recommended Ollama env vars to set

## After install: required Ollama env vars

These are critical for stability. Set them in your shell profile (or via
the Windows installer, which sets them automatically at User scope):

```bash
export OLLAMA_KEEP_ALIVE=-1            # never unload the pinned model
export OLLAMA_MAX_LOADED_MODELS=1      # one model at a time (24 GB GPU)
export OLLAMA_NUM_PARALLEL=1           # one concurrent request
export OLLAMA_HOST=0.0.0.0:11434       # accept LAN connections
```

**Restart Ollama after setting these.** Without them you risk OOM, slow
output-layer-on-CPU spills, and unpredictable model evictions.

## opencode config

Point opencode at the proxy port (`18000`), not Ollama directly. Use the
**renamed** model id `local-coder:35b-a3b` — opencode's v1.14.33 source
hardcodes `temperature=0.55, top_p=1` for any model whose id contains
"qwen", which overrides this Modelfile's tuned values; the rename bypasses
that branch.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "ollama": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Ollama Local",
      "options": {
        "baseURL": "http://YOUR_SERVER_IP:18000/v1"
      },
      "models": {
        "local-coder:35b-a3b": {
          "name": "Qwen3.6 35b (via proxy)",
          "limit": {
            "context": 60000,
            "output": 8000
          }
        }
      }
    }
  }
}
```

(See `examples/opencode-config.json`.)

## Manual install (if you don't want to run a script)

```bash
# 1. Pull the base model
ollama pull qwen3.6:35b-a3b

# 2. Build the customised model from the Modelfile in this repo
ollama create local-coder:35b-a3b -f ./local-coder.Modelfile

# 3. Set the Ollama env vars listed above and restart Ollama

# 4. Install Python deps
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# 5. Run the proxy in foreground
./.venv/bin/python qwen_coder_proxy.py
# -> proxy on 0.0.0.0:18000

# 6. Verify
curl -s http://localhost:18000/v1/models
```

## Service management

**Windows**:
```powershell
Stop-ScheduledTask  -TaskName "qwen-coder-proxy"
Start-ScheduledTask -TaskName "qwen-coder-proxy"
Get-ScheduledTaskInfo -TaskName "qwen-coder-proxy"
```

**macOS**:
```bash
launchctl unload ~/Library/LaunchAgents/com.user.qwen-coder-proxy.plist
launchctl load   ~/Library/LaunchAgents/com.user.qwen-coder-proxy.plist
launchctl list | grep qwen-coder-proxy
```

**Linux**:
```bash
systemctl --user status   qwen-coder-proxy
systemctl --user restart  qwen-coder-proxy
journalctl --user -u qwen-coder-proxy -f
```

The proxy log is at `<install_dir>/proxy.log` on all platforms.

## Configuration

The proxy reads three environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `QWEN_PROXY_HOST`     | `0.0.0.0`               | Bind address |
| `QWEN_PROXY_PORT`     | `18000`                 | Listen port |
| `QWEN_PROXY_UPSTREAM` | `http://127.0.0.1:11434` | Where to forward requests |

The Modelfile parameters can be tuned by editing `local-coder.Modelfile` and
running `ollama create local-coder:35b-a3b -f local-coder.Modelfile` again.
Notable defaults:

* `num_ctx 60000` — 60k context (kv-cache q8_0 quantized to fit on 24 GB)
* `temperature 0.3` — low for tool-call reliability without locking the model
  into a single response shape
* `top_p 0.9`, `top_k 20` — Qwen-recommended

## Tests

The proxy ships with built-in unit tests for the parser and the response
transformer:

```bash
python qwen_coder_proxy.py --test
```

## Troubleshooting

**Model returns text-only, no tool calls**
Likely the proxy is not in the path. Verify opencode's `baseURL` points
at port `18000`, not `11434`. Check `proxy.log` to confirm requests are
arriving.

**Empty `<think></think>` in chat instead of real reasoning**
Ollama's OpenAI-compat layer auto-suppresses thinking when `tools` are
present in the request. The proxy already handles this by extracting the
reasoning into `reasoning_content`. If you see literal tags in the chat
UI, opencode v1.14.33 doesn't strip them — opencode-side display issue,
not a proxy bug.

**`tool_calls` malformed, or strings get wrapped in extra quotes**
You're likely on an older Modelfile. Rebuild:
```bash
ollama create local-coder:35b-a3b -f ./local-coder.Modelfile
```
The current Modelfile uses a conditional `{{ if eq (printf "%T" $value) "string" }}` to keep strings as raw text and only JSON-encode complex values.

**Schema validation errors from opencode (e.g. bash missing description)**
The proxy auto-fills missing required fields. If you still see schema
errors, check `proxy.log` and confirm the proxy version (`/v1/models`
response should reach the proxy first).

**Output layer offloaded to CPU — slow generation**
Expected at 60k context on a 24 GB GPU. The proxy itself is unaffected;
generation throughput is around 30–35 tok/s once prefill completes. If
you want fully-on-GPU, lower `num_ctx` in the Modelfile (e.g. to 32000)
and rebuild.

**First message in a new conversation takes 10–30s before any tokens come back**
That's prompt prefill — opencode sends ~5,000–7,000 tokens of system prompt
+ tool defs + history that all have to be encoded before generation can
start. Subsequent turns reuse the kv-cache and feel snappy.

**On Windows, the Scheduled Task starts but exits immediately on boot**
The bundled `start_proxy.bat` uses `pythonw.exe` (no console window) and
`start /B` to detach so the task itself completes cleanly. If you've
modified it to use `python.exe` directly, the task can receive boot-time
console signals and die. Revert to the shipped wrapper.

## Uninstall

**Windows**:
```powershell
Stop-ScheduledTask -TaskName "qwen-coder-proxy"
Unregister-ScheduledTask -TaskName "qwen-coder-proxy" -Confirm:$false
Remove-Item -Recurse "$env:USERPROFILE\ollama-proxy-qwen"
ollama rm local-coder:35b-a3b
```

**macOS**:
```bash
launchctl unload ~/Library/LaunchAgents/com.user.qwen-coder-proxy.plist
rm ~/Library/LaunchAgents/com.user.qwen-coder-proxy.plist
rm -rf ~/ollama-proxy-qwen
ollama rm local-coder:35b-a3b
```

**Linux**:
```bash
systemctl --user disable --now qwen-coder-proxy
rm ~/.config/systemd/user/qwen-coder-proxy.service
rm -rf ~/ollama-proxy-qwen
ollama rm local-coder:35b-a3b
```

## License

MIT — see [LICENSE](LICENSE).
