@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=D:\msys64\ucrt64\bin\python.exe"
if exist "%~dp0cacert.pem" set "SSL_CERT_FILE=%~dp0cacert.pem"
if not defined SSL_CERT_FILE if exist "D:\msys64\ucrt64\ssl\certs\ca-bundle.crt" set "SSL_CERT_FILE=D:\msys64\ucrt64\ssl\certs\ca-bundle.crt"
if not defined SSL_CERT_FILE if exist "D:\msys64\etc\ssl\certs\ca-bundle.crt" set "SSL_CERT_FILE=D:\msys64\etc\ssl\certs\ca-bundle.crt"

if exist "%~dp0ObsidianVowPcAgent.exe" goto packaged

if exist "%PYTHON_EXE%" (
  "%PYTHON_EXE%" agent.py
) else (
  python agent.py
)
exit /b %ERRORLEVEL%

:packaged
"%~dp0ObsidianVowPcAgent.exe"
exit /b %ERRORLEVEL%
