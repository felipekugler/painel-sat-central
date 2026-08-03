"""Painel SAT Central — painel autônomo com a feature "Arquivos SAT": baixa os
documentos previdenciários de um assistido no SAT Central (INSS/Dataprev) e os
anexa a um PAJ do SISDPU (movimentação → tramitação). Os PAJs são incluídos
manualmente (busca global no SISDPU por número).

Auto-gerencia processo: mata qualquer python na porta antes de iniciar, grava
PID file. Evita duplicatas.
"""

import contextlib
import logging
import os
import subprocess
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import jinja2
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from routes.cofre import router as cofre_router
from routes.sat import router as sat_router

BASE_DIR = Path(__file__).resolve().parent
PID_FILE = BASE_DIR / ".server.pid"
LOGS_DIR = BASE_DIR / "logs"
PORT = 8003

APP_VERSION = "1.0.0"


def _configurar_logging() -> None:
    LOGS_DIR.mkdir(exist_ok=True)
    handler = TimedRotatingFileHandler(
        LOGS_DIR / "app.log", when="midnight", backupCount=7, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, TimedRotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)


_configurar_logging()

app = FastAPI(title="painel-sat-central", version=APP_VERSION)
app.state.jinja = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=True,
    auto_reload=True,
)
app.state.jinja.globals["app_version"] = APP_VERSION
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(sat_router)
app.include_router(cofre_router)


@app.on_event("shutdown")
async def _shutdown_fechar_navegadores() -> None:
    """Ao encerrar o painel (graciosamente), fecha os navegadores do SAT e do
    SISDPU junto. NÃO roda em kill -9 (Stop-Process -Force), então reinícios de
    dev preservam as sessões (cookies de login)."""
    with contextlib.suppress(Exception):
        from ingestao import sat_client
        sat_client.fechar_navegador()
    with contextlib.suppress(Exception):
        from ingestao import sisdpu_client
        await sisdpu_client.fechar()


def _cleanup_port(port: int) -> None:
    """Mata TODOS processos LISTEN na porta informada (Windows-only)."""
    if os.name != "nt":
        return
    try:
        result = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True)
    except Exception:
        return
    seen: set[int] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[3] != "LISTENING":
            continue
        if not parts[1].endswith(f":{port}"):
            continue
        try:
            pid = int(parts[-1])
        except ValueError:
            continue
        if pid <= 4 or pid == os.getpid() or pid in seen:
            continue
        seen.add(pid)
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        print(f"[app] killed stale process on port {port}: PID {pid}")


def _kill_pid_file() -> None:
    if not PID_FILE.exists():
        return
    try:
        old_pid = int(PID_FILE.read_text().strip())
        if old_pid != os.getpid():
            subprocess.run(["taskkill", "/F", "/PID", str(old_pid)], capture_output=True)
            print(f"[app] killed previous server PID {old_pid}")
    except (ValueError, FileNotFoundError):
        pass
    with contextlib.suppress(Exception):
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    _kill_pid_file()
    _cleanup_port(PORT)
    import time

    time.sleep(1)  # aguarda SO liberar a porta

    PID_FILE.write_text(str(os.getpid()))
    print(f"[app] servidor PID={os.getpid()} — http://127.0.0.1:{PORT}")
    try:
        uvicorn.run(app, host="127.0.0.1", port=PORT, reload=False)
    finally:
        with contextlib.suppress(Exception):
            PID_FILE.unlink(missing_ok=True)
        with contextlib.suppress(Exception):
            from ingestao import sat_client
            sat_client.fechar_navegador()
