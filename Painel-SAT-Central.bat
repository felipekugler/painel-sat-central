@echo off
rem ============================================================
rem  Painel SAT Central — inicia o servidor (se precisar) e abre o navegador
rem  Usar: dar duplo clique, ou criar atalho no desktop/barra de tarefas
rem ============================================================

setlocal

set ROOT=%~dp0
cd /d "%ROOT%"

set PYTHON=%ROOT%.venv\Scripts\python.exe
set URL=http://127.0.0.1:8003/

if not exist "%PYTHON%" (
    echo [ERRO] venv nao encontrado em %PYTHON%
    echo Crie com:  py -3 -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt ^&^& .venv\Scripts\python -m playwright install chromium
    pause
    goto :fim
)

netstat -ano -p TCP | findstr /R /C:":8003 .* LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo Servidor ja esta rodando. Abrindo navegador...
    start "" "%URL%"
    goto :fim
)

echo Iniciando Painel SAT Central...
start "Painel SAT Central" /MIN "%PYTHON%" "%ROOT%app.py"

timeout /T 4 /NOBREAK >nul

echo Abrindo navegador em %URL%
start "" "%URL%"

:fim
