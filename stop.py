#!/usr/bin/env python3
"""Para o servidor do Painel SAT Central (lê PID file + limpa a porta)."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PID_FILE = ROOT / ".server.pid"
PORT = 8003


def _killp_windows(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)


def _killp_posix(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)


def _sweep_porta_windows(port: int) -> None:
    result = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True)
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
        if pid > 4 and pid not in seen:
            _killp_windows(pid)
            seen.add(pid)
            print(f"[stop] matou processo na porta {port}: PID {pid}")


def _sweep_porta_posix(port: int) -> None:
    try:
        result = subprocess.run(["lsof", "-ti", f":{port}", "-sTCP:LISTEN"], capture_output=True, text=True)
    except FileNotFoundError:
        print(f"[stop] (aviso) lsof não encontrado — pulando sweep da porta {port}")
        return
    seen: set[int] = set()
    for linha in result.stdout.splitlines():
        try:
            pid = int(linha.strip())
        except ValueError:
            continue
        if pid in seen or pid == os.getpid():
            continue
        _killp_posix(pid)
        seen.add(pid)
        print(f"[stop] matou processo na porta {port}: PID {pid}")


def main() -> int:
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            (_killp_windows if os.name == "nt" else _killp_posix)(pid)
            print(f"[stop] matou PID {pid} (do PID file)")
        except ValueError:
            pass
        PID_FILE.unlink(missing_ok=True)

    (_sweep_porta_windows if os.name == "nt" else _sweep_porta_posix)(PORT)
    print("[stop] servidor parado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
