@echo off
REM Wrapper for the Windows Scheduled Task that launches qwen-coder-proxy
REM detached as pythonw.exe (no console window, immune to Ctrl+C signals).
REM Edit INSTALL_DIR if you placed the proxy somewhere other than the default.

set "INSTALL_DIR=%USERPROFILE%\ollama-proxy-qwen"
set "LOG=%INSTALL_DIR%\proxy.log"
set "PYTHONW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"

if not exist "%PYTHONW%" (
  REM fall back to python on PATH
  set "PYTHONW=pythonw.exe"
)

cd /d "%INSTALL_DIR%"
echo [%date% %time%] proxy starting (pythonw, no console) >> "%LOG%"
start "" /B "%PYTHONW%" qwen_coder_proxy.py 1>> "%LOG%" 2>&1
