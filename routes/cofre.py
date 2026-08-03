"""API do cofre de credenciais (`services.cofre_service`) — protege o usuário/
senha do SISDPU com uma senha-mestra. Não tranca a navegação do painel: o
desbloqueio é sob demanda (modal ao acionar algo que precise do SISDPU).

Endpoints:
- GET    /api/cofre/status            → estado do cofre + sistemas com credencial
- POST   /api/cofre/desbloquear       → {senha_mestra}
- POST   /api/cofre/bloquear
- POST   /api/cofre/credenciais       → {sistema, usuario, senha, senha_mestra[, confirma]}
- DELETE /api/cofre/credenciais/{sistema} → {senha_mestra}
- POST   /api/cofre/trocar-senha      → {senha_atual, senha_nova[, confirma]}
"""

from __future__ import annotations

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from services import cofre_service

router = APIRouter()

_MIN_SENHA_MESTRA = 6


@router.get("/api/cofre/status", response_class=JSONResponse)
async def cofre_status():
    """Estado do cofre. Enquanto BLOQUEADO, só se sabe que existe — a lista de
    sistemas só aparece após desbloquear."""
    destrancado = cofre_service.destrancado()
    sistemas: dict = {}
    for chave, rotulo in cofre_service.SISTEMAS.items():
        sistemas[chave] = {
            "rotulo": rotulo,
            "tem_credencial": cofre_service.tem_credencial(chave),
            "usuario": cofre_service.get_usuario(chave),
        }
    return {
        "ok": True,
        "configurado": cofre_service.configurado(),
        "destrancado": destrancado,
        "sistemas": sistemas,
    }


@router.post("/api/cofre/desbloquear", response_class=JSONResponse)
async def cofre_desbloquear(payload: dict = Body(...)):
    senha_mestra = (payload or {}).get("senha_mestra", "")
    if not senha_mestra:
        return JSONResponse({"ok": False, "erro": "Informe a senha-mestra."}, status_code=400)
    if cofre_service.destrancar(senha_mestra):
        return {"ok": True, "sistemas": cofre_service.sistemas_configurados()}
    return JSONResponse({"ok": False, "erro": "Senha-mestra incorreta."}, status_code=401)


@router.post("/api/cofre/bloquear", response_class=JSONResponse)
async def cofre_bloquear():
    """Esquece todas as credenciais da memória (tranca de novo)."""
    cofre_service.bloquear()
    return {"ok": True}


@router.post("/api/cofre/credenciais", response_class=JSONResponse)
async def cofre_salvar(payload: dict = Body(...)):
    """Cadastra/atualiza as credenciais de UM sistema. Se o cofre já existe, a
    senha-mestra deve conferir com ele."""
    p = payload or {}
    sistema = (p.get("sistema") or "").strip().lower()
    usuario = (p.get("usuario") or "").strip()
    senha = p.get("senha") or ""
    senha_mestra = p.get("senha_mestra") or ""
    confirma = p.get("senha_mestra_confirma")

    if sistema not in cofre_service.SISTEMAS:
        return JSONResponse(
            {"ok": False, "erro": f"Sistema desconhecido: {sistema!r}."}, status_code=400)
    if not usuario or not senha or not senha_mestra:
        return JSONResponse(
            {"ok": False, "erro": "Preencha usuário, senha e senha-mestra."}, status_code=400)
    if len(senha_mestra) < _MIN_SENHA_MESTRA:
        return JSONResponse(
            {"ok": False, "erro": f"A senha-mestra deve ter ao menos {_MIN_SENHA_MESTRA} caracteres."},
            status_code=400)
    # Confirmação só é exigida quando o cofre AINDA não existe (primeira criação).
    if not cofre_service.configurado() and confirma is not None and senha_mestra != confirma:
        return JSONResponse(
            {"ok": False, "erro": "A confirmação da senha-mestra não confere."}, status_code=400)

    try:
        cofre_service.salvar_sistema(sistema, usuario, senha, senha_mestra)
    except cofre_service.SenhaMestraIncorreta:
        return JSONResponse(
            {"ok": False, "erro": "A senha-mestra não confere com o cofre já existente. "
                                  "Use a mesma senha-mestra do cofre."}, status_code=401)
    except ValueError as e:
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=500)
    return {"ok": True, "sistemas": cofre_service.sistemas_configurados()}


@router.delete("/api/cofre/credenciais/{sistema}", response_class=JSONResponse)
async def cofre_remover(sistema: str, payload: dict = Body(default=None)):
    """Remove um sistema do cofre (mantém os demais). Requer a senha-mestra no
    corpo para re-cifrar o cofre restante."""
    sistema = (sistema or "").strip().lower()
    senha_mestra = (payload or {}).get("senha_mestra", "") if payload else ""
    if not senha_mestra:
        return JSONResponse(
            {"ok": False, "erro": "Informe a senha-mestra para remover a credencial."},
            status_code=400)
    try:
        removeu = cofre_service.remover_sistema(sistema, senha_mestra)
    except cofre_service.SenhaMestraIncorreta:
        return JSONResponse({"ok": False, "erro": "Senha-mestra incorreta."}, status_code=401)
    if not removeu:
        return JSONResponse(
            {"ok": False, "erro": "Sistema não estava no cofre."}, status_code=404)
    return {"ok": True}


@router.post("/api/cofre/trocar-senha", response_class=JSONResponse)
async def cofre_trocar_senha(payload: dict = Body(...)):
    """Re-cifra o cofre inteiro com uma nova senha-mestra."""
    p = payload or {}
    senha_atual = p.get("senha_atual") or ""
    senha_nova = p.get("senha_nova") or ""
    confirma = p.get("senha_nova_confirma")
    if not senha_atual or not senha_nova:
        return JSONResponse(
            {"ok": False, "erro": "Informe a senha-mestra atual e a nova."}, status_code=400)
    if len(senha_nova) < _MIN_SENHA_MESTRA:
        return JSONResponse(
            {"ok": False, "erro": f"A nova senha-mestra deve ter ao menos {_MIN_SENHA_MESTRA} caracteres."},
            status_code=400)
    if confirma is not None and senha_nova != confirma:
        return JSONResponse(
            {"ok": False, "erro": "A confirmação da nova senha-mestra não confere."}, status_code=400)
    try:
        cofre_service.trocar_senha_mestra(senha_atual, senha_nova)
    except cofre_service.SenhaMestraIncorreta:
        return JSONResponse(
            {"ok": False, "erro": "A senha-mestra atual está incorreta."}, status_code=401)
    return {"ok": True}
