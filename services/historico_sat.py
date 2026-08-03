"""Histórico dos PAJs trabalhados no fluxo "Arquivos SAT".

Um JSONL append-only (uma linha por PAJ processado) em
`OFICIO_GERAL/_historico_sat.jsonl`. Cada registro guarda o resultado de cada
etapa (download → movimentação → tramitação → conclusão) e o log passo-a-passo,
para a tabela de histórico da aba "Arquivos SAT".

Formato do registro (campos principais):
  id, ts, paj, paj_norm, id_processo, id_tramite, assistido, cpf,
  n_arquivos, arquivos[], mov_status, mov_seq, mov_url, mov_detalhe,
  tram_status, tram_caixa, tram_detalhe, concluido_status, concluido_detalhe,
  status (geral: concluido|parcial|falha|dry_run), detalhe, log[]
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import config

_ARQ = Path(config.OFICIO_GERAL) / "_historico_sat.jsonl"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def registrar(record: dict) -> dict:
    """Append de um registro (gera id/ts se faltarem)."""
    record.setdefault("id", uuid.uuid4().hex[:12])
    record.setdefault("ts", _now_iso())
    _ARQ.parent.mkdir(parents=True, exist_ok=True)
    with _ARQ.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def listar() -> list[dict]:
    """Todos os registros, mais recentes primeiro."""
    if not _ARQ.exists():
        return []
    out: list[dict] = []
    for linha in _ARQ.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha:
            continue
        try:
            out.append(json.loads(linha))
        except Exception:  # noqa: BLE001
            continue
    out.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return out


def atualizar(rec_id: str, campos: dict[str, Any]) -> dict | None:
    """Reescreve o JSONL preservando ordem, aplicando `campos` no registro `rec_id`."""
    if not _ARQ.exists():
        return None
    linhas = _ARQ.read_text(encoding="utf-8").splitlines()
    novo: list[str] = []
    alvo: dict | None = None
    for linha in linhas:
        linha = linha.strip()
        if not linha:
            continue
        try:
            r = json.loads(linha)
        except Exception:  # noqa: BLE001
            novo.append(linha)
            continue
        if r.get("id") == rec_id:
            r.update(campos)
            alvo = r
        novo.append(json.dumps(r, ensure_ascii=False))
    _ARQ.write_text("\n".join(novo) + "\n", encoding="utf-8")
    return alvo


def por_id_tramite(id_tramite: str) -> dict | None:
    """Último registro (mais recente) para um id_tramite — usado p/ marcar 'já tratado'."""
    for r in listar():
        if str(r.get("id_tramite") or "") == str(id_tramite):
            return r
    return None
