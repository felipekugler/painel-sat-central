"""Cofre de credenciais protegido por uma única SENHA-MESTRA.

Guarda o usuário/senha do SISDPU cifrados em disco. A senha-mestra NUNCA é
gravada em lugar nenhum — só existe na memória do processo enquanto o cofre
está destrancado (ao reiniciar o painel, é preciso desbloquear de novo).

Cripto: Fernet (AES-128-CBC + HMAC-SHA256) com chave derivada por PBKDF2-HMAC-SHA256
(salt aleatório por arquivo, 240k iterações). Biblioteca `cryptography`.

Formato do arquivo (JSON de uma linha):
    {"v": 1, "salt": "<b64>", "token": "<fernet>"}
onde o plaintext cifrado é:
    {"sistemas": {"sisdpu": {"usuario": "...", "senha": "..."}}}

O cofre é genérico (aceita qualquer chave de sistema), mas este painel só usa
"sisdpu" — o SAT não entra aqui, o login (CPF/CAPTCHA/2FA) é feito pelo
usuário na janela.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

import config

CRED_FILE = Path(config.OFICIO_GERAL) / "_cofre.enc"

_ITERACOES = 240_000  # custo do PBKDF2 (dificulta força bruta da senha-mestra)

# Sistemas conhecidos (rótulos para exibição). O cofre em si é agnóstico e
# aceita qualquer chave — mas este painel só cadastra o SISDPU.
SISTEMAS = {
    "sisdpu": "SISDPU",
}


class SenhaMestraIncorreta(Exception):
    """A senha-mestra não decifra o cofre existente."""


# Estado em MEMÓRIA da sessão atual (nunca persistido).
# sistemas = { "<sistema>": {"usuario": "...", "senha": "..."} }
_estado: dict = {"destrancado": False, "sistemas": {}}


def _derivar_chave(senha_mestra: str, salt: bytes) -> bytes:
    """Deriva a chave Fernet (32 bytes url-safe base64) a partir da senha-mestra."""
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_ITERACOES)
    return base64.urlsafe_b64encode(kdf.derive(senha_mestra.encode("utf-8")))


def _ler_cofre(senha_mestra: str) -> dict:
    """Lê e decifra o cofre em disco com a senha-mestra dada. Retorna o dict de
    sistemas ({} se o arquivo não existe). Levanta SenhaMestraIncorreta se a
    senha-mestra não confere / o arquivo está corrompido."""
    if not CRED_FILE.exists():
        return {}
    try:
        d = json.loads(CRED_FILE.read_text(encoding="utf-8"))
        salt = base64.b64decode(d["salt"])
        bruto = Fernet(_derivar_chave(senha_mestra or "", salt)).decrypt(d["token"].encode("utf-8"))
        dados = json.loads(bruto.decode("utf-8"))
    except (InvalidToken, KeyError, ValueError, json.JSONDecodeError) as e:
        raise SenhaMestraIncorreta() from e
    except Exception as e:  # noqa: BLE001 — qualquer falha = senha errada / corrompido
        raise SenhaMestraIncorreta() from e
    sistemas = dados.get("sistemas", {})
    return sistemas if isinstance(sistemas, dict) else {}


def _gravar_cofre(sistemas: dict, senha_mestra: str) -> None:
    """Cifra o dict de sistemas com a senha-mestra e grava em disco (novo salt)."""
    CRED_FILE.parent.mkdir(parents=True, exist_ok=True)
    salt = os.urandom(16)
    token = Fernet(_derivar_chave(senha_mestra, salt)).encrypt(
        json.dumps({"sistemas": sistemas}).encode("utf-8"))
    CRED_FILE.write_text(
        json.dumps({"v": 1, "salt": base64.b64encode(salt).decode(), "token": token.decode()}),
        encoding="utf-8")


# --- Consulta de estado -----------------------------------------------------
def configurado() -> bool:
    """True se já existe um cofre cifrado em disco (independe de estar destrancado)."""
    return CRED_FILE.exists()


def destrancado() -> bool:
    """True se o cofre já foi decifrado nesta sessão (credenciais em memória)."""
    return bool(_estado["destrancado"])


def sistemas_configurados() -> list[str]:
    """Lista os sistemas com credencial no cofre. Só conhecido quando destrancado."""
    if not _estado["destrancado"]:
        return []
    return sorted(_estado["sistemas"].keys())


def tem_credencial(sistema: str) -> bool:
    """True se o cofre (destrancado) tem credencial para o sistema dado."""
    return bool(_estado["destrancado"] and _estado["sistemas"].get(sistema))


# --- Desbloqueio / bloqueio -------------------------------------------------
def destrancar(senha_mestra: str) -> bool:
    """Decifra o cofre com a senha-mestra e carrega tudo em memória. Retorna True
    se acertou; False se a senha-mestra estiver errada (ou o arquivo inválido)."""
    if not CRED_FILE.exists():
        return False
    try:
        sistemas = _ler_cofre(senha_mestra)
    except SenhaMestraIncorreta:
        return False
    _estado.update(destrancado=True, sistemas=sistemas)
    return True


def bloquear() -> None:
    """Esquece as credenciais da memória (tranca de novo). O cofre em disco fica."""
    _estado.update(destrancado=False, sistemas={})


# --- Cadastro / remoção de credenciais --------------------------------------
def salvar_sistema(sistema: str, usuario: str, senha: str, senha_mestra: str) -> None:
    """Adiciona/atualiza as credenciais de UM sistema no cofre, mantendo os demais
    intactos. Deixa a sessão destrancada.

    Se o cofre já existe, a senha-mestra fornecida DEVE conferir com ele (senão
    levanta SenhaMestraIncorreta). Se não existe, este vira o cofre inicial.
    """
    sistema = (sistema or "").strip().lower()
    usuario = (usuario or "").strip()
    senha = senha or ""
    if not sistema:
        raise ValueError("Sistema é obrigatório.")
    if not usuario or not senha or not (senha_mestra or ""):
        raise ValueError("Usuário, senha e senha-mestra são obrigatórios.")

    if CRED_FILE.exists():
        sistemas = _ler_cofre(senha_mestra)  # levanta SenhaMestraIncorreta se não confere
    elif _estado["destrancado"]:
        sistemas = dict(_estado["sistemas"])
    else:
        sistemas = {}

    sistemas[sistema] = {"usuario": usuario, "senha": senha}
    _gravar_cofre(sistemas, senha_mestra)
    _estado.update(destrancado=True, sistemas=sistemas)


def remover_sistema(sistema: str, senha_mestra: str) -> bool:
    """Remove as credenciais de UM sistema do cofre (mantém os demais). Retorna
    True se removeu; levanta SenhaMestraIncorreta se a senha-mestra não confere.
    Se após a remoção o cofre ficar vazio, apaga o arquivo."""
    sistema = (sistema or "").strip().lower()
    if not CRED_FILE.exists():
        return False
    sistemas = _ler_cofre(senha_mestra)  # valida senha-mestra
    if sistema not in sistemas:
        return False
    del sistemas[sistema]
    if sistemas:
        _gravar_cofre(sistemas, senha_mestra)
    else:
        try:
            CRED_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    if _estado["destrancado"]:
        _estado["sistemas"].pop(sistema, None)
    return True


def trocar_senha_mestra(senha_atual: str, senha_nova: str) -> None:
    """Re-cifra o cofre inteiro com uma nova senha-mestra. Levanta
    SenhaMestraIncorreta se `senha_atual` não confere."""
    if not (senha_nova or ""):
        raise ValueError("A nova senha-mestra é obrigatória.")
    sistemas = _ler_cofre(senha_atual)  # valida a atual
    _gravar_cofre(sistemas, senha_nova)
    _estado.update(destrancado=True, sistemas=sistemas)


def remover_tudo() -> None:
    """Bloqueia e APAGA o cofre inteiro em disco (todos os sistemas)."""
    bloquear()
    try:
        CRED_FILE.unlink(missing_ok=True)
    except OSError:
        pass


# --- Uso pelas rotinas de acesso --------------------------------------------
def get_credenciais(sistema: str) -> tuple[str, str] | None:
    """(usuario, senha) do sistema se o cofre está destrancado e tem a credencial;
    None caso contrário."""
    sistema = (sistema or "").strip().lower()
    if not _estado["destrancado"]:
        return None
    cred = _estado["sistemas"].get(sistema)
    if cred and cred.get("usuario") and cred.get("senha"):
        return cred["usuario"], cred["senha"]
    return None


def get_usuario(sistema: str) -> str:
    """Usuário do sistema carregado (só quando destrancado) — para exibição."""
    cred = get_credenciais(sistema)
    return cred[0] if cred else ""
