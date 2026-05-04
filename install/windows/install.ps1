# install.ps1 — Windows installer for qwen3.6 + qwen-coder-proxy + opencode
#
# Run from an elevated PowerShell:
#   Set-ExecutionPolicy -Scope Process Bypass
#   .\install.ps1
#
# Steps:
#   1. Verify prerequisites (Ollama, Python)
#   2. Pull qwen3.6:35b-a3b
#   3. Build local-coder:35b-a3b from the Modelfile
#   4. Install Python deps for the proxy (fastapi, uvicorn, httpx)
#   5. Copy proxy + bat to %USERPROFILE%\ollama-proxy-qwen
#   6. Set Ollama env vars (KEEP_ALIVE=-1, MAX_LOADED_MODELS=1, HOST, NUM_PARALLEL=1)
#   7. Register a Scheduled Task that auto-starts the proxy at logon

param(
    [string]$InstallDir = "$env:USERPROFILE\ollama-proxy-qwen",
    [string]$ProxyPort = "18000",
    [switch]$SkipModelPull = $false,
    [switch]$SkipModelBuild = $false
)

$ErrorActionPreference = "Stop"

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    OK $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    !! $msg" -ForegroundColor Yellow }

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)

Step "Prerequisite check: ollama"
$ollama = (Get-Command ollama -ErrorAction SilentlyContinue)
if (-not $ollama) {
    throw "ollama not found on PATH. Install from https://ollama.com/download"
}
Ok "ollama: $($ollama.Source)"

Step "Prerequisite check: python"
$python = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $python) {
    throw "python not found on PATH. Install from https://www.python.org/downloads/"
}
Ok "python: $($python.Source)"

Step "Creating install dir: $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item "$RepoRoot\qwen_coder_proxy.py" -Destination $InstallDir -Force
Copy-Item "$RepoRoot\local-coder.Modelfile" -Destination $InstallDir -Force
Copy-Item "$RepoRoot\requirements.txt" -Destination $InstallDir -Force
$Bat = Get-Content "$RepoRoot\install\windows\start_proxy.bat" -Raw
$Bat = $Bat -replace '%USERPROFILE%\\ollama-proxy-qwen', $InstallDir
Set-Content -Path "$InstallDir\start_proxy.bat" -Value $Bat -Encoding ASCII
Ok "files copied"

Step "Installing Python dependencies"
& python -m pip install --user -r "$InstallDir\requirements.txt" | Out-Null
Ok "fastapi, uvicorn, httpx installed"

if (-not $SkipModelPull) {
    Step "Pulling qwen3.6:35b-a3b (~22 GB — this can take a while)"
    & ollama pull qwen3.6:35b-a3b
    Ok "model pulled"
} else {
    Warn "skipping model pull (-SkipModelPull)"
}

if (-not $SkipModelBuild) {
    Step "Building local-coder:35b-a3b from Modelfile"
    & ollama create local-coder:35b-a3b -f "$InstallDir\local-coder.Modelfile"
    Ok "local-coder:35b-a3b created"
} else {
    Warn "skipping model build (-SkipModelBuild)"
}

Step "Setting persistent Ollama env vars (User scope)"
[Environment]::SetEnvironmentVariable('OLLAMA_KEEP_ALIVE', '-1', 'User')
[Environment]::SetEnvironmentVariable('OLLAMA_MAX_LOADED_MODELS', '1', 'User')
[Environment]::SetEnvironmentVariable('OLLAMA_NUM_PARALLEL', '1', 'User')
[Environment]::SetEnvironmentVariable('OLLAMA_HOST', '0.0.0.0:11434', 'User')
Ok "OLLAMA_KEEP_ALIVE=-1, MAX_LOADED_MODELS=1, NUM_PARALLEL=1, HOST=0.0.0.0:11434"
Warn "restart ollama for env vars to take effect"

Step "Registering Scheduled Task 'qwen-coder-proxy'"
$taskName = "qwen-coder-proxy"
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction -Execute "$InstallDir\start_proxy.bat"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Description "qwen-coder-proxy: converts Qwen3.6 qwen3_coder XML tool calls to OpenAI tool_calls (port $ProxyPort -> 11434)" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal | Out-Null
Ok "task registered"

Step "Starting the proxy now"
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 4
$running = Get-NetTCPConnection -LocalPort $ProxyPort -State Listen -ErrorAction SilentlyContinue
if ($running) {
    Ok "proxy listening on 0.0.0.0:$ProxyPort"
} else {
    Warn "proxy did not bind — check $InstallDir\proxy.log"
}

Write-Host ""
Write-Host "=== install complete ===" -ForegroundColor Green
Write-Host "  install dir : $InstallDir"
Write-Host "  proxy port  : $ProxyPort"
Write-Host "  log file    : $InstallDir\proxy.log"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Restart Ollama so the new env vars take effect"
Write-Host "  2. Point opencode at http://YOUR_IP:$ProxyPort/v1"
Write-Host "     (see examples/opencode-config.json)"
Write-Host "  3. Use model id  local-coder:35b-a3b"
