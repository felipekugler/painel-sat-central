"""Estado da conexão do painel com o SISDPU (para o selo de status + Reconectar
na barra superior).

As credenciais vêm SEMPRE do cofre cifrado (senha-mestra) — não há fallback
via `.env`. O estado de conexão é REATIVO: as rotinas de acesso ao SISDPU
(sisdpu_movtram / sisdpu_client) chamam `marcar_conflito`, `marcar_conectado`
ou `marcar_erro` conforme detectam o diálogo de conflito de sessão, um login
bem-sucedido, ou uma falha. O selo lê este estado via GET /api/seguranca/status.
"""

from __future__ import annotations

from datetime import datetime

_estado: dict = {
    "conflito": False,
    "detalhe": "",
    "erro": "",
    "conectado": False,
    "atualizado_em": "",
}


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def marcar_conflito(detalhe: str = "") -> None:
    _estado["conflito"] = True
    _estado["detalhe"] = detalhe or "Conflito de sessão do SISDPU (login em outro lugar)."
    _estado["conectado"] = False
    _estado["atualizado_em"] = _agora()


def marcar_conectado() -> None:
    _estado["conflito"] = False
    _estado["detalhe"] = ""
    _estado["erro"] = ""
    _estado["conectado"] = True
    _estado["atualizado_em"] = _agora()


def marcar_erro(detalhe: str = "") -> None:
    _estado["erro"] = detalhe or "Falha ao acessar o SISDPU."
    _estado["conectado"] = False
    _estado["atualizado_em"] = _agora()


def cofre_bloqueado() -> bool:
    """True se há um cofre configurado mas ainda bloqueado (sem credenciais do
    SISDPU em memória). Nesse caso o selo deve indicar que é preciso desbloquear
    antes de usar."""
    from services import cofre_service
    return bool(cofre_service.configurado() and not cofre_service.tem_credencial("sisdpu"))


def credenciais_disponiveis() -> bool:
    """True se há credenciais do SISDPU prontas para uso (cofre destrancado)."""
    from services import cofre_service
    return bool(cofre_service.get_credenciais("sisdpu"))


def status() -> dict:
    """Snapshot para o selo de status. `configurado` = há credenciais prontas para
    uso (cofre destrancado). `bloqueado` = cofre existe mas ainda não foi
    desbloqueado."""
    return {
        "ok": True,
        "configurado": credenciais_disponiveis(),
        "bloqueado": cofre_bloqueado(),
        "conflito": bool(_estado["conflito"]),
        "conflito_detalhe": _estado["detalhe"],
        "conectado": bool(_estado["conectado"]),
        "erro": _estado["erro"],
        "atualizado_em": _estado["atualizado_em"],
    }
