"""Serviço da feature "Arquivos SAT" (Painel SAT Central).

Orquestra, para um PAJ incluído MANUALMENTE (busca global no SISDPU por
número — este painel não lê a caixa de entrada automaticamente):

    baixar os PDFs do SAT Central (Grupo 1/2 + PAT, via ingestao.sat_client) →
    movimentação (fase "Juntada de documento", anexos = PDFs como
    "Documentos do Assistido") → tramitação (caixa configurável).

Como o PAJ não vem de um trâmite da caixa, NÃO há conclusão (não existe
trâmite originário para concluir) — o fluxo termina na tramitação. Os PDFs
ficam em `PAJS_DIR/<paj_norm>/Arquivos SAT/` e o progresso é transmitido via
SSE, com o resultado gravado em `historico_sat`.
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import shutil
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator

import config
from ingestao import sisdpu_movtram as movtram
from services import historico_sat

# --- parâmetros de negócio ---
FASE_MOV = "Juntada de documento"
TIPO_ARQUIVO = "Documentos do Assistido"
MOV_DESCRICAO = "Juntada dos documentos obtidos junto ao SAT Central (INSS/Dataprev)"
MOV_DESCRICAO_COMPLEMENTAR = ("Juntada complementar de documentos obtidos junto ao SAT "
                              "Central (INSS/Dataprev)")
TRAM_DESCRICAO = "Documentos do SAT Central juntados ao PAJ"
GRUPO_PADRAO = "01 - PREV_DIVPREV"
# Sentinela de "grupo" que representa a opção "Não tramitar" (só movimenta/anexa
# os documentos; pula a etapa de tramitação por completo).
NAO_TRAMITAR = "__nao_tramitar__"
SUBPASTA_SAT = "Arquivos SAT"
SUBPASTA_INSTITUIDOR = "Instituidor"  # dentro de "Arquivos SAT" (download/anexação à parte)
SUBPASTA_COMPLEMENTAR = "Complementar"  # download complementar do assistido (anexado por movimentação)
SUBPASTA_COMPLEMENTAR_INSTITUIDOR = "Complementar Instituidor"  # idem, do instituidor
# Originais dos PDFs compilados por tipo (espelha sat_client.SUBPASTA_ORIGINAIS_COMPILADOS).
# Ficam fora da anexação; apagados só APÓS a conclusão da tramitação (ver processar_paj_stream).
SUBPASTA_ORIGINAIS_COMPILADOS = "Compilados (originais)"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().lower()


def _extrair_pretensao_pura(pretensao: str) -> str:
    """Parte após ' >> ' da pretensão do SISDPU, removendo a área temática."""
    if not pretensao:
        return ""
    partes = pretensao.split(">>")
    return partes[1].strip() if len(partes) > 1 else pretensao.strip()


def _num_to_norm(paj: str) -> str:
    """'2026/029-01651' -> 'PAJ-2026-029-01651'."""
    m = re.match(r"(\d{4})/(\d{3})-(\d+)", paj or "")
    return f"PAJ-{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else (paj or "")


def _norm_to_num(paj_norm: str) -> str:
    """'PAJ-2026-029-01651' -> '2026/029-01651'."""
    m = re.match(r"PAJ-(\d{4})-(\d{3})-(\d+)", paj_norm or "")
    return f"{m.group(1)}/{m.group(2)}-{m.group(3)}" if m else (paj_norm or "")


def _pasta_paj(paj_norm: str) -> Path:
    return Path(config.PAJS_DIR) / paj_norm


def _ler_metadata(paj_norm: str) -> dict:
    p = _pasta_paj(paj_norm) / "metadata.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _cpf_do_paj(paj_norm: str) -> str:
    meta = _ler_metadata(paj_norm)
    raw = (meta.get("sisdpu_raw") or {})
    return (raw.get("cpf_assistido") or "").strip()


def _html_para_texto(h: str) -> str:
    """Converte o HTML da narrativa do SISDPU em texto limpo, preservando parágrafos."""
    if not h:
        return ""
    h = re.sub(r"(?i)<\s*br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</\s*(p|div|tr|li)\s*>", "\n", h)
    h = re.sub(r"<[^>]+>", "", h)          # remove as tags restantes
    h = _html.unescape(h)
    h = h.replace("\xa0", " ")
    h = re.sub(r"[ \t]+", " ", h)
    h = re.sub(r" *\n *", "\n", h)
    h = re.sub(r"\n{3,}", "\n\n", h)
    return h.strip()


def narrativa_paj(paj_norm: str) -> dict:
    """Texto da narrativa do PAJ. Este painel não tem sincronização completa
    (sem metadata.json) — a narrativa vem do que foi capturado ao vivo na busca
    global do SISDPU quando o PAJ foi cadastrado manualmente."""
    meta = _ler_metadata(paj_norm)
    raw = (meta.get("sisdpu_raw") or {})
    texto = _html_para_texto(raw.get("narrativa") or "")
    if not texto:
        mp = next((m for m in _ler_pajs_manuais() if m.get("paj_norm") == paj_norm), None)
        texto = (mp or {}).get("narrativa", "")
    return {"paj_norm": paj_norm, "texto": texto}


def pasta_arquivos_sat(paj_norm: str) -> Path:
    """Pasta destino dos PDFs do SAT para um PAJ."""
    return _pasta_paj(paj_norm) / SUBPASTA_SAT


def listar_arquivos_sat(paj_norm: str) -> list[dict]:
    """PDFs já baixados na subpasta 'Arquivos SAT' (ignora o _LOG e não-PDF)."""
    pasta = pasta_arquivos_sat(paj_norm)
    if not pasta.exists():
        return []
    out = []
    for f in sorted(pasta.glob("*.pdf")):
        try:
            tam = f.stat().st_size
        except Exception:  # noqa: BLE001
            tam = 0
        out.append({"nome": f.name, "caminho": str(f), "tipo": "pdf", "tamanho": tam})
    return out


def pasta_instituidor(paj_norm: str) -> Path:
    """Subpasta dos documentos do INSTITUIDOR (download e anexação SEPARADOS)."""
    return pasta_arquivos_sat(paj_norm) / SUBPASTA_INSTITUIDOR


def listar_arquivos_instituidor(paj_norm: str) -> list[dict]:
    """PDFs baixados do instituidor (subpasta 'Instituidor')."""
    pasta = pasta_instituidor(paj_norm)
    if not pasta.exists():
        return []
    out = []
    for f in sorted(pasta.glob("*.pdf")):
        try:
            tam = f.stat().st_size
        except Exception:  # noqa: BLE001
            tam = 0
        out.append({"nome": f.name, "caminho": str(f), "tipo": "pdf", "tamanho": tam})
    return out


# marcador (normalizado) → key do tipo, para reconstruir o inventário de logs antigos
_MARCADOR_KEY = [
    ("cnis - dados cadastrais", "cnis_dados_cadastrais"),
    ("cnis - atividades", "cnis_atividades"),
    ("cnis - elos", "cnis_elos"),
    ("cnis - microfichas", "cnis_microfichas"),
    ("cnis - remuneracoes", "cnis_remuneracoes"),
    ("cnis - vinculos", "cnis_vinculos"),
    ("declaracao de beneficios", "declaracao_beneficios"),
    ("requerimentos do sibe", "requerimentos_sibe"),
    ("revisao de beneficio", "revisao_art29"),
    ("hisconsignados", "consignados"),
    ("hiscre", "hiscre"),
    ("laudo social", "laudo_social"),
    ("laudo medico", "laudo_medico"),
    ("procadm", "pat"),
    ("tarefa", "pat"),
    ("ccon", "carta_concessao"),
    ("crer", "crer"),
]


def _key_de_texto(txt: str) -> str:
    t = _norm(txt)
    for marc, key in _MARCADOR_KEY:
        if marc in t:
            return key
    return ""


def _sub_de_texto(txt: str, key: str) -> str:
    if key == "pat":
        m = re.search(r"prot\s+(\d{5,})", txt, re.I) or re.search(r"tarefa\s+(\d{5,})", txt, re.I)
        return m.group(1) if m else ""
    m = re.search(r"\d{3}\.\d{3}\.\d{3}-\d(?!\d)", txt)
    return m.group(0) if m else ""


def _status_de_pulado(txt: str) -> str:
    t = _norm(txt)
    if "relacionado" in t:
        return "nao_relacionado"
    if "nao consultado" in t:
        return "nao_consultado"
    if "antirob" in t or "sem resposta" in t:
        return "antirobo"
    if "indisponiv" in t or "inacessiv" in t:
        return "indisponivel"
    if "falha" in t:
        return "falha"
    if "sem dados" in t or "sem linhas" in t or "sem tarefas" in t or "nao retornou" in t:
        return "sem_dados"
    return "sem_dados"


def _inventario_de_log(d: dict) -> list[dict]:
    """Fallback: reconstrói o inventário a partir de salvos[]/pulados[] de logs antigos."""
    inv: list[dict] = []
    for nome in d.get("salvos", []) or []:
        key = _key_de_texto(nome)
        if key:
            inv.append({"key": key, "sub": _sub_de_texto(nome, key), "nome": nome, "status": "baixado"})
    for p in d.get("pulados", []) or []:
        key = _key_de_texto(p)
        if not key:
            continue
        inv.append({"key": key, "sub": _sub_de_texto(p, key), "nome": "", "status": _status_de_pulado(p)})
    return inv


_STATUS_FALHA = {"falha", "antirobo", "indisponivel"}


def _download_tem_falha(paj_norm: str) -> bool:
    """True se o último log de download registrou FALHA real (status de erro no inventário
    ou evento de nível 'erro'). Skips intencionais (ignorado/pulado/sem_dados/etc.) NÃO
    contam como falha."""
    p = pasta_arquivos_sat(paj_norm) / "_log_download.json"
    if not os.path.isfile(str(p)):
        return False
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    if any(it.get("status") in _STATUS_FALHA for it in (d.get("inventario") or [])):
        return True
    return any(ev.get("level") == "erro" for ev in (d.get("eventos") or []))


def _pat_servico_map(d: dict) -> dict:
    """Mapa {protocolo: serviço} do PAT, reconstruído dos nomes/textos do log — para exibir
    o tipo de serviço no "Baixar mais" mesmo em logs sem o campo `rotulo`.
    Nomes de arquivo: 'PROCADM <Serviço> - ... PROT <prot>'; pulados: 'PAT <Serviço>
    (protocolo <prot>)'."""
    mapa: dict[str, str] = {}
    for nome in d.get("salvos", []) or []:
        m = re.search(r"PROCADM\s+(.+?)\s+-\s+.*?PROT\s+(\d{5,})", nome, re.I)
        if m:
            mapa.setdefault(m.group(2), m.group(1).strip())
    for p in d.get("pulados", []) or []:
        m = re.search(r"PAT\s+(.+?)\s+\(protocolo\s+(\d{5,})\)", p, re.I)
        if m:
            mapa.setdefault(m.group(2), m.group(1).strip())
    return mapa


def inventario_paj(paj_norm: str, escopo: str = "assistido") -> dict:
    """Inventário dos documentos revelados no SAT (1º download) + marcas ja_baixado /
    ja_anexado. Usa `inventario` do log (novos) ou reconstrói de salvos/pulados (antigos).
    `escopo` = 'assistido' (pasta principal) ou 'instituidor' (subpasta 'Instituidor')."""
    paths = _escopo_paths(paj_norm, escopo)
    pasta_base, listar_escopo = paths["destino"], paths["listar"]
    log_p = pasta_base / "_log_download.json"
    cpf, inv, pat_serv = "", [], {}
    if os.path.isfile(str(log_p)):
        try:
            d = json.loads(log_p.read_text(encoding="utf-8"))
            cpf = d.get("cpf", "") or ""
            inv = d.get("inventario") or _inventario_de_log(d)
            pat_serv = _pat_servico_map(d)  # {protocolo: serviço} p/ rótulo do PAT
        except Exception:  # noqa: BLE001
            inv = []
    if not cpf and escopo == "assistido":
        cpf = _cpf_do_paj(paj_norm)
    if not cpf and escopo == "assistido":
        mp = next((m for m in _ler_pajs_manuais() if m.get("paj_norm") == paj_norm), None)
        cpf = (mp or {}).get("cpf", "")
    # já anexados: nome -> conjunto de movs em que foi anexado, fazendo MERGE de todas as
    # anexações registradas neste PAJ (movimentação principal + cada "anexar mais").
    anexado_movs: dict[str, set] = {}
    for r in historico_sat.listar():
        if r.get("paj_norm") != paj_norm:
            continue
        for a in (r.get("arquivos") or []):
            anexado_movs.setdefault(a, set()).add(r.get("mov_seq"))
        for m in (r.get("movs_extra") or []):
            for a in (m.get("arquivos") or []):
                anexado_movs.setdefault(a, set()).add(m.get("seq"))
    anexados: set[str] = set(anexado_movs.keys())
    # já baixados: arquivos presentes nas pastas (principal + instituidor + complementar)
    baixados: set[str] = set()
    for lst in (listar_arquivos_sat(paj_norm), listar_arquivos_instituidor(paj_norm),
                listar_arquivos_complementar(paj_norm)):
        for a in lst:
            baixados.add(a["nome"])
    # GROUND TRUTH (auto-reparo): todo PDF presente na pasta do escopo FOI baixado. Injeta
    # itens "baixado" a partir dos arquivos no disco — assim, se um merge antigo tiver
    # rebaixado um item para "nao_selecionado", o dedup abaixo (baixado vence) o recupera.
    for a in listar_escopo(paj_norm):
        nome_a = a["nome"]
        key_a = _key_de_texto(nome_a)
        if not key_a:
            continue
        sub_a = _sub_de_texto(nome_a, key_a)
        inv.append({"key": key_a, "sub": sub_a, "nome": nome_a, "status": "baixado",
                    "rotulo": (pat_serv.get(sub_a, "") if key_a == "pat" else "")})
    # dedup por (key, sub) — a paginação do PAT pode revelar o mesmo protocolo 2x; itens
    # duplicados quebravam o x-for do "Baixar mais" (chave repetida). Preferência de status:
    # baixado > qualquer outro (mantém o registro mais informativo).
    _PRIO = {"baixado": 3}
    dedup: dict[tuple, dict] = {}
    for it in inv:
        chave = (it.get("key", ""), it.get("sub", ""))
        atual = dedup.get(chave)
        if atual is None:
            dedup[chave] = dict(it)
            continue
        # preserva o rótulo/serviço se o vencedor atual não tiver
        if not atual.get("rotulo") and it.get("rotulo"):
            atual["rotulo"] = it["rotulo"]
        # troca o vencedor SÓ quando o novo tem status de prioridade MAIOR (baixado manda)
        if _PRIO.get(it.get("status", ""), 0) > _PRIO.get(atual.get("status", ""), 0):
            novo = dict(it)
            if not novo.get("rotulo") and atual.get("rotulo"):
                novo["rotulo"] = atual["rotulo"]
            dedup[chave] = novo
    itens = []
    for it in dedup.values():
        nome = it.get("nome", "")
        key, sub = it.get("key", ""), it.get("sub", "")
        servico = it.get("rotulo", "") or (pat_serv.get(sub, "") if key == "pat" else "")
        itens.append({
            "key": key, "sub": sub, "nome": nome, "servico": servico,
            "status": it.get("status", ""),
            "ja_baixado": bool(it.get("status") == "baixado" and (not nome or nome in baixados)),
            "ja_anexado": bool(nome and nome in anexados),
            "anexado_movs": sorted(s for s in anexado_movs.get(nome, set()) if s is not None),
        })
    return {"paj_norm": paj_norm, "cpf": cpf, "itens": itens}


def pasta_complementar(paj_norm: str) -> Path:
    """Subpasta do download COMPLEMENTAR (baixado depois, anexado por movimentação)."""
    return pasta_arquivos_sat(paj_norm) / SUBPASTA_COMPLEMENTAR


def listar_arquivos_complementar(paj_norm: str) -> list[dict]:
    """PDFs na subpasta 'Complementar'."""
    pasta = pasta_complementar(paj_norm)
    if not pasta.exists():
        return []
    out = []
    for f in sorted(pasta.glob("*.pdf")):
        try:
            tam = f.stat().st_size
        except Exception:  # noqa: BLE001
            tam = 0
        out.append({"nome": f.name, "caminho": str(f), "tipo": "pdf", "tamanho": tam})
    return out


def pasta_excluidos(paj_norm: str) -> Path:
    """Subpasta-lixeira dos documentos do ASSISTIDO removidos da lista (soft delete)."""
    return pasta_arquivos_sat(paj_norm) / "Excluídos"


def pasta_excluidos_instituidor(paj_norm: str) -> Path:
    """Lixeira dos documentos do INSTITUIDOR removidos (dentro de 'Instituidor')."""
    return pasta_instituidor(paj_norm) / "Excluídos"


def tem_instituidor(paj_norm: str) -> bool:
    """Há instituidor neste PAJ? (subpasta 'Instituidor' com log de download ou PDFs)."""
    pi = pasta_instituidor(paj_norm)
    return bool((pi / "_log_download.json").exists() or listar_arquivos_instituidor(paj_norm))


def _escopo_paths(paj_norm: str, escopo: str) -> dict:
    """Resolve pastas/rótulo/listagem conforme o escopo ('assistido' | 'instituidor').
    Assim o download/mescla complementar serve tanto o acervo do assistido (pasta principal)
    quanto o do instituidor (subpasta 'Instituidor')."""
    if escopo == "instituidor":
        return {
            "destino": pasta_instituidor(paj_norm),
            "complementar": pasta_arquivos_sat(paj_norm) / SUBPASTA_COMPLEMENTAR_INSTITUIDOR,
            "listar": listar_arquivos_instituidor,
            "rotulo": "INSTITUIDOR - ",
        }
    return {
        "destino": pasta_arquivos_sat(paj_norm),
        "complementar": pasta_complementar(paj_norm),
        "listar": listar_arquivos_sat,
        "rotulo": "",
    }


def _cpf_de_log(pasta: Path) -> str:
    """Lê o CPF gravado no _log_download.json de uma pasta de download (assistido/instituidor)."""
    p = pasta / "_log_download.json"
    if not os.path.isfile(str(p)):
        return ""
    try:
        return (json.loads(p.read_text(encoding="utf-8")).get("cpf") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _referencia(escopo: str, cpf: str) -> str:
    """Rótulo da coluna 'Referência': 'Assistido - CPF X' / 'Instituidor - CPF X'."""
    quem = "Assistido" if escopo == "assistido" else "Instituidor"
    return f"{quem} - CPF {cpf}" if cpf else quem


def _listar_pdfs_pasta(pasta: Path, escopo: str, referencia: str, excluido: bool) -> list[dict]:
    out: list[dict] = []
    if not pasta.exists():
        return out
    for f in sorted(pasta.glob("*.pdf")):
        try:
            tam = f.stat().st_size
        except Exception:  # noqa: BLE001
            tam = 0
        out.append({"nome": f.name, "caminho": str(f), "tipo": "pdf", "tamanho": tam,
                    "excluido": excluido, "escopo": escopo, "referencia": referencia})
    return out


def listar_todos_arquivos_sat(paj_norm: str) -> list[dict]:
    """Tabela UNIFICADA para a UI: documentos do ASSISTIDO (pasta principal) + do
    INSTITUIDOR (subpasta 'Instituidor'), ativos e excluídos, cada um com `escopo`,
    `referencia` ('Assistido/Instituidor - CPF X') e flag `excluido`."""
    cpf_a = _cpf_do_paj(paj_norm) or _cpf_de_log(pasta_arquivos_sat(paj_norm))
    cpf_i = _cpf_de_log(pasta_instituidor(paj_norm))
    ref_a, ref_i = _referencia("assistido", cpf_a), _referencia("instituidor", cpf_i)
    out: list[dict] = []
    out += _listar_pdfs_pasta(pasta_arquivos_sat(paj_norm), "assistido", ref_a, False)
    out += _listar_pdfs_pasta(pasta_instituidor(paj_norm), "instituidor", ref_i, False)
    out += _listar_pdfs_pasta(pasta_excluidos(paj_norm), "assistido", ref_a, True)
    out += _listar_pdfs_pasta(pasta_excluidos_instituidor(paj_norm), "instituidor", ref_i, True)
    return out


def _mover_pdf(origem: Path, destino_dir: Path) -> None:
    destino_dir.mkdir(parents=True, exist_ok=True)
    destino = destino_dir / origem.name
    if destino.exists():
        destino.unlink()
    shutil.move(str(origem), str(destino))


def _contagens(paj_norm: str) -> dict:
    return {"n_arquivos": len(listar_arquivos_sat(paj_norm)),
            "n_instituidor": len(listar_arquivos_instituidor(paj_norm))}


def excluir_arquivo_sat(paj_norm: str, nome: str, escopo: str = "assistido") -> dict:
    """SOFT DELETE: move o documento (do assistido OU do instituidor, conforme `escopo`)
    para a respectiva lixeira 'Excluídos'. Não some do disco; pode ser recuperado."""
    inst = (escopo == "instituidor")
    origem = (pasta_instituidor if inst else pasta_arquivos_sat)(paj_norm).resolve()
    destino = pasta_excluidos_instituidor(paj_norm) if inst else pasta_excluidos(paj_norm)
    alvo = (origem / Path(nome or "").name).resolve()
    if origem not in alvo.parents or not alvo.exists():
        return {"ok": False, "detalhe": "Arquivo não encontrado.", **_contagens(paj_norm)}
    try:
        _mover_pdf(alvo, destino)
        return {"ok": True, "detalhe": f"Removido da lista: {alvo.name}", **_contagens(paj_norm)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detalhe": str(e), **_contagens(paj_norm)}


def recuperar_arquivo_sat(paj_norm: str, nome: str, escopo: str = "assistido") -> dict:
    """Traz o documento de volta da lixeira para a pasta de origem (assistido/instituidor)."""
    inst = (escopo == "instituidor")
    exc = (pasta_excluidos_instituidor(paj_norm) if inst else pasta_excluidos(paj_norm)).resolve()
    destino = pasta_instituidor(paj_norm) if inst else pasta_arquivos_sat(paj_norm)
    alvo = (exc / Path(nome or "").name).resolve()
    if exc not in alvo.parents or not alvo.exists():
        return {"ok": False, "detalhe": "Arquivo não encontrado em Excluídos.", **_contagens(paj_norm)}
    try:
        _mover_pdf(alvo, destino)
        return {"ok": True, "detalhe": f"Recuperado: {alvo.name}", **_contagens(paj_norm)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detalhe": str(e), **_contagens(paj_norm)}


# ---------------------------------------------------------------------------
# PAJs adicionados MANUALMENTE — única forma de incluir PAJs neste painel (não
# há varredura automática da caixa de entrada).
# ---------------------------------------------------------------------------
_ARQ_MANUAIS = Path(config.OFICIO_GERAL) / "_pajs_manuais.json"


def _parse_paj_input(txt: str):
    """Aceita 'AAAA/UUU-NNNNN', 'AAAA-UUU-NNNNN', 'PAJ-AAAA-UUU-NNNNN' ou 12 dígitos.
    Retorna (paj_norm, ano, unidade, numero_seq) ou None se não reconhecer."""
    s = re.sub(r"\s+", "", txt or "")
    m = re.search(r"(\d{4})\D{0,3}(\d{3})\D{0,3}(\d{3,})", s)
    if not m:
        return None
    ano, uni, num = m.group(1), m.group(2), m.group(3)
    return (f"PAJ-{ano}-{uni}-{num}", ano, uni, num)


def _ler_pajs_manuais() -> list[dict]:
    if not _ARQ_MANUAIS.exists():
        return []
    try:
        return json.loads(_ARQ_MANUAIS.read_text(encoding="utf-8")) or []
    except Exception:  # noqa: BLE001
        return []


def _salvar_pajs_manuais(lst: list[dict]) -> None:
    _ARQ_MANUAIS.parent.mkdir(parents=True, exist_ok=True)
    _ARQ_MANUAIS.write_text(json.dumps(lst, ensure_ascii=False, indent=2), encoding="utf-8")


def listar_pajs_manuais() -> list[dict]:
    return _ler_pajs_manuais()


async def add_paj_manual(texto: str) -> dict:
    """Adiciona um PAJ à lista extraindo os dados AO VIVO do SISDPU (busca global,
    mesma sessão headless). O PAJ só é aceito se localizado e com CPF extraído.
    Retorna {ok, item} ou {ok:False, erro}."""
    parsed = _parse_paj_input(texto)
    if not parsed:
        return {"ok": False, "erro": f"Número de PAJ inválido: '{texto}'. Use AAAA/UUU-NNNNN."}
    paj_norm, ano, uni, num = parsed
    numero = f"{ano}/{uni}-{num}"
    from ingestao import sisdpu_client as _sc
    try:
        # forcar_sessao=False: reaproveita a sessão viva (login automático só se expirou),
        # em vez de fechar e relogar do zero a cada inclusão (que era lento e frágil).
        dados = await _sc.buscar_paj_global(num, ano, uni, forcar_sessao=False)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"Falha na autenticação/consulta ao SISDPU: {e}. "
                                     f"Verifique as credenciais do SISDPU no cofre (Configurações)."}
    if not isinstance(dados, dict) or dados.get("erro"):
        return {"ok": False, "erro": (dados or {}).get("erro") or "PAJ não localizado no SISDPU."}
    cpf = (dados.get("cpf_assistido") or "").strip()
    if not cpf:
        return {"ok": False, "erro": "PAJ localizado, mas não consegui extrair o CPF do assistido."}
    url = dados.get("url", "") or ""
    mid = re.search(r"[?&]id=(\d+)", url)
    id_processo = mid.group(1) if mid else ""
    pret = (dados.get("pretensao") or "").strip()
    pret = (_extrair_pretensao_pura(pret) or pret).strip()
    item = {
        "paj_norm": paj_norm, "numero": numero, "id_processo": id_processo,
        "cpf": cpf, "assistido": (dados.get("assistido") or "").strip(),
        "pretensao": pret, "narrativa": _html_para_texto(dados.get("narrativa") or ""),
        "url_detalhe": url,
    }
    lst = [x for x in _ler_pajs_manuais() if x.get("paj_norm") != paj_norm]
    lst.append(item)
    _salvar_pajs_manuais(lst)
    return {"ok": True, "item": item}


def del_paj_manual(paj_norm: str, apagar_arquivos: bool = True, registrar_historico: bool = True) -> dict:
    """Remove o PAJ da lista. Por padrão (uso pelo botão ✕) TAMBÉM apaga a pasta
    com os arquivos já baixados (PAJS_DIR/<paj_norm>/) e registra a exclusão em
    "PAJs Trabalhados" — ação irreversível, o front-end deve confirmar com o
    usuário antes de chamar com os padrões.

    Uso interno (limpeza automática da lista após "Anexar ao PAJ" ter sucesso,
    em processar_paj_stream) passa apagar_arquivos=False, registrar_historico=False:
    os arquivos já foram anexados ao PAJ (não faz sentido apagá-los aqui) e o
    próprio processamento já registrou seu resultado no histórico — registrar
    de novo como "exclusão manual" seria enganoso."""
    lst = _ler_pajs_manuais()
    mp = next((x for x in lst if x.get("paj_norm") == paj_norm), None)
    _salvar_pajs_manuais([x for x in lst if x.get("paj_norm") != paj_norm])
    pasta = _pasta_paj(paj_norm)
    tinha_arquivos = pasta.exists()
    if apagar_arquivos and tinha_arquivos:
        shutil.rmtree(pasta, ignore_errors=True)
    if registrar_historico and mp:
        detalhe = ("PAJ removido manualmente da lista pelo usuário; os arquivos já "
                    "baixados do SAT foram excluídos." if tinha_arquivos else
                    "PAJ removido manualmente da lista pelo usuário (não havia arquivos baixados).")
        historico_sat.registrar({
            "paj": mp.get("numero") or _norm_to_num(paj_norm),
            "paj_norm": paj_norm,
            "id_processo": mp.get("id_processo", ""),
            "assistido": mp.get("assistido", ""),
            "cpf": mp.get("cpf", ""),
            "url_detalhe": mp.get("url_detalhe", ""),
            "escopo": "exclusao_manual",
            "mov_status": "nao_aplicavel",
            "tram_status": "nao_aplicavel",
            "concluido_status": "nao_aplicavel",
            "status": "excluido",
            "detalhe": detalhe,
            "log": [{"level": "muted", "msg": detalhe}],
        })
    return {"ok": True}


def _item_pendencia_manual(mp: dict) -> dict:
    """Monta o item da tabela a partir de um PAJ manual do store."""
    pn = mp.get("paj_norm", "")
    arquivos = listar_arquivos_sat(pn)
    return {
        "paj": mp.get("numero") or _norm_to_num(pn),
        "paj_norm": pn,
        "id_processo": mp.get("id_processo", ""),
        "id_tramite": "",
        "assistido": mp.get("assistido", ""),
        "cpf": mp.get("cpf", ""),
        "pretensao": mp.get("pretensao", ""),
        "descricao": "",
        "url_detalhe": mp.get("url_detalhe", ""),
        "n_arquivos": len(arquivos),
        "n_instituidor": len(listar_arquivos_instituidor(pn)),
        "tem_pasta": _pasta_paj(pn).exists(),
        "sincronizado": bool(_ler_metadata(pn)),
        "tem_log": (pasta_arquivos_sat(pn) / "_log_download.json").exists(),
        "tem_falha": _download_tem_falha(pn),
        "ja_processado": False,
        "manual": True,
        "narrativa": mp.get("narrativa", ""),
    }


def listar_pendentes_manuais() -> dict:
    """Todos os PAJs cadastrados manualmente (única fonte de pendências deste painel —
    não há varredura da caixa de entrada). Rápido e offline (não toca o SISDPU)."""
    itens = [_item_pendencia_manual(mp) for mp in _ler_pajs_manuais() if mp.get("paj_norm")]
    return {"ok": True, "itens": itens}


# ---------------------------------------------------------------------------
# processamento (SSE) — movimentar → tramitar (SEM conclusão: PAJ manual não
# tem trâmite originário na caixa para concluir)
# ---------------------------------------------------------------------------
def _ev(level: str, msg: str) -> dict:
    return {"level": level, "msg": msg}


def _passo_ev(etapa: str, ok: bool | None, detalhe: str) -> dict:
    icone = "✓" if ok else ("✗" if ok is False else "•")
    level = "ok" if ok else ("erro" if ok is False else "muted")
    return {"level": level, "msg": f"     {icone} {etapa}" + (f": {detalhe}" if detalhe else "")}


async def processar_paj_stream(
    paj_norm: str,
    grupo: str | None = None,
    prazo_dias: int = 0,
    dry_run: bool = False,
) -> AsyncIterator[dict]:
    """Gera eventos SSE do processamento de UM PAJ manual: movimenta → tramita.
    Assume os PDFs já baixados em 'Arquivos SAT'. `prazo_dias` > 0 define prazo de
    conclusão da tramitação (hoje + N dias); 0 = sem prazo. Termina com
    {'done': True, 'resultado': {...}}."""
    nao_tramitar = (grupo == NAO_TRAMITAR)
    if not nao_tramitar:
        grupo = grupo or GRUPO_PADRAO
    prazo_data = None
    if prazo_dias and int(prazo_dias) > 0 and not nao_tramitar:
        prazo_data = (date.today() + timedelta(days=int(prazo_dias))).strftime("%d/%m/%Y")
    log: list[dict] = []

    def passo(etapa: str, ok: bool | None, detalhe: str) -> dict:
        log.append({"ts": datetime.now().isoformat(timespec="seconds"),
                    "etapa": etapa, "ok": (None if ok is None else bool(ok)),
                    "detalhe": str(detalhe or "")[:600]})
        return _passo_ev(etapa, ok, detalhe)

    num = _norm_to_num(paj_norm)
    prefixo = "[dry-run] " if dry_run else ""
    destino_txt = "sem tramitação" if nao_tramitar else f"grupo destino: {grupo}"
    yield _ev("novo", f"{prefixo}Processando {num} ({destino_txt})")

    mp = next((m for m in _ler_pajs_manuais() if m.get("paj_norm") == paj_norm), None)
    if not mp or not mp.get("id_processo"):
        d = (f"PAJ {num} sem id do processo. Remova-o e adicione novamente "
             f"(a extração do SISDPU obtém o id).")
        yield passo("Localização do PAJ", False, d)
        yield {"done": True, "resultado": {"ok": False, "status": "falha", "detalhe": d}}
        return
    id_proc = mp["id_processo"]
    item = {"id": id_proc, "id_tramite": "", "assistido": mp.get("assistido", "")}
    yield passo("Localização do PAJ", True, f"id_processo={id_proc}")

    # anexos = TUDO que estiver ATIVO na tabela: documentos do ASSISTIDO (pasta principal) +
    # do INSTITUIDOR (subpasta), numa única movimentação.
    anexos_assist = listar_arquivos_sat(paj_norm)
    anexos_inst = listar_arquivos_instituidor(paj_norm)
    anexos = anexos_assist + anexos_inst
    if not anexos:
        d = (f"Nenhum PDF em '{SUBPASTA_SAT}' para {num}. Baixe os documentos do SAT antes "
             f"de processar (pasta: {pasta_arquivos_sat(paj_norm)}).")
        yield passo("Documentos do SAT", False, d)
        yield {"done": True, "resultado": {"ok": False, "status": "falha", "detalhe": d}}
        return
    _det_anexos = f"{len(anexos)} arquivo(s) para juntar"
    if anexos_inst:
        _det_anexos += f" ({len(anexos_assist)} do assistido + {len(anexos_inst)} do instituidor)"
    yield passo("Documentos do SAT", True, _det_anexos)

    pajs = [{"id": id_proc, "numero": num}]
    resultado: dict[str, Any] = {"paj": num, "paj_norm": paj_norm, "grupo": grupo}

    # 1) MOVIMENTAR
    yield _ev("info", "Movimentando (juntada de documento)...")
    mv = await movtram.movimentar_paj(pajs, MOV_DESCRICAO, FASE_MOV, anexos, TIPO_ARQUIVO, dry_run)
    yield passo("Movimentação do PAJ (Juntada de documento)", mv.get("ok"), mv.get("detalhe", ""))
    resultado["mov_status"] = mv.get("status")
    resultado["mov_seq"] = mv.get("mov_seq")
    resultado["mov_detalhe"] = mv.get("detalhe", "")
    if not mv.get("ok"):
        _gravar_hist(paj_norm, item, anexos, ("" if nao_tramitar else grupo), resultado,
                     "falha", "Falha na movimentação.", log)
        yield {"done": True, "resultado": {"ok": False, "status": "falha", "detalhe": mv.get("detalhe", "")}}
        return

    # 2) TRAMITAR (pulado se o usuário escolheu "Não tramitar")
    if nao_tramitar:
        yield passo("Tramitação", None,
                    "Não realizada — opção \"Não tramitar\" escolhida pelo usuário.")
        resultado["tram_status"] = "nao_tramitado"
        resultado["tram_caixa"] = ""
        resultado["tram_detalhe"] = "Tramitação não realizada — opção \"Não tramitar\" escolhida."
        tr_ok = True  # a movimentação (anexação) já teve sucesso; não há tramitação a avaliar
    else:
        prazo_txt = f"prazo até {prazo_data}" if prazo_data else "sem prazo"
        yield _ev("info", f"Tramitando para {grupo} ({prazo_txt})...")
        tr = await movtram.tramitar_paj(pajs, grupo=grupo, descricao=TRAM_DESCRICAO,
                                        prazo_data=prazo_data, dry_run=dry_run)
        yield passo(f"Tramitação para {grupo} ({prazo_txt})", tr.get("ok"), tr.get("detalhe", ""))
        resultado["tram_status"] = tr.get("status")
        resultado["tram_caixa"] = grupo
        resultado["tram_detalhe"] = tr.get("detalhe", "")
        tr_ok = tr.get("ok")

    resultado["concluido_status"] = "nao_aplicavel"
    resultado["concluido_detalhe"] = "PAJ manual — sem trâmite na caixa para concluir."
    yield passo("Conclusão", None,
                "PAJ incluído manualmente: não há trâmite na caixa para concluir — etapa ignorada.")
    anexacao_ok = tr_ok
    status = "dry_run" if dry_run else ("concluido" if tr_ok else "parcial")
    detalhe = ("[dry-run] Simulação concluída — nada foi efetivado." if dry_run
               else ("Movimentado (sem tramitação — opção \"Não tramitar\")." if (nao_tramitar and tr_ok)
                     else ("Movimentado e tramitado (conclusão não se aplica a PAJ manual)."
                           if tr_ok else "Movimentou, mas falhou a tramitação.")))

    # Compilação: apagar os PDFs originais SÓ APÓS a anexação (o compilado já foi anexado na
    # movimentação; os originais ficaram numa subpasta não-anexada até aqui).
    if anexacao_ok and not dry_run:
        orig = pasta_arquivos_sat(paj_norm) / SUBPASTA_ORIGINAIS_COMPILADOS
        if orig.is_dir():
            n = len(list(orig.glob("*.pdf")))
            try:
                shutil.rmtree(orig, ignore_errors=True)
                yield passo("Limpeza dos originais compilados", True,
                            f"{n} PDF(s) originais removidos após a anexação do(s) compilado(s).")
            except Exception as e:  # noqa: BLE001
                yield passo("Limpeza dos originais compilados", None, str(e))
    _gravar_hist(paj_norm, item, anexos, ("" if nao_tramitar else grupo), resultado, status, detalhe, log)
    # PAJ processado com sucesso sai da lista de pendências (some do store).
    if status == "concluido" and not dry_run:
        with __import__("contextlib").suppress(Exception):
            del_paj_manual(paj_norm, apagar_arquivos=False, registrar_historico=False)
    yield {"done": True, "resultado": {"ok": bool(dry_run or anexacao_ok), "status": status, "detalhe": detalhe}}


async def anexar_mais_stream(
    paj_norm: str, rec_id: str, id_processo: str, assistido: str, nomes: list[str],
) -> AsyncIterator[dict]:
    """Anexação COMPLEMENTAR num PAJ já concluído: nova movimentação (SÓ juntada) com os
    documentos escolhidos — SEM tramitar nem concluir. Complementa o registro `rec_id` do
    histórico (lista `movs_extra` + `log`) para exibir o badge da nova movimentação na aba
    'PAJs Trabalhados'."""
    num = _norm_to_num(paj_norm)
    log_ev: list[dict] = []

    def passo(etapa: str, ok, det: str) -> dict:
        log_ev.append({"ts": datetime.now().isoformat(timespec="seconds"), "etapa": etapa,
                       "ok": (None if ok is None else bool(ok)), "detalhe": str(det or "")[:600]})
        return _passo_ev(etapa, ok, det)

    yield _ev("novo", f"Anexação complementar em {num}…")
    # resolve os anexos escolhidos — TODOS os documentos baixados são passíveis de nova
    # anexação (ativos E excluídos da lixeira; assistido + instituidor).
    disponiveis = {d["nome"]: d for d in listar_todos_arquivos_sat(paj_norm)}
    anexos = [{"nome": n, "caminho": disponiveis[n]["caminho"], "tipo": "pdf"}
              for n in (nomes or []) if n in disponiveis]
    if not anexos:
        yield passo("Documentos", False, "Nenhum documento válido selecionado.")
        yield {"done": True, "resultado": {"ok": False, "detalhe": "Nenhum documento selecionado."}}
        return
    yield passo("Documentos", True, f"{len(anexos)} documento(s) para anexar")
    if not id_processo:
        yield passo("Movimentação", False, "Sem id do processo no registro.")
        yield {"done": True, "resultado": {"ok": False, "detalhe": "Sem id do processo."}}
        return
    yield _ev("info", "Movimentando (anexação complementar)…")
    mv = await movtram.movimentar_paj([{"id": id_processo, "numero": num}],
                                      MOV_DESCRICAO_COMPLEMENTAR, FASE_MOV, anexos, TIPO_ARQUIVO, False)
    yield passo("Movimentação complementar", mv.get("ok"), mv.get("detalhe", ""))
    if not mv.get("ok"):
        yield {"done": True, "resultado": {"ok": False, "detalhe": mv.get("detalhe", "")}}
        return
    # complementa o registro existente (badge da nova mov + log)
    extra = {"seq": mv.get("mov_seq"), "ts": datetime.now().isoformat(timespec="seconds"),
             "n": len(anexos), "arquivos": [a["nome"] for a in anexos]}
    with __import__("contextlib").suppress(Exception):
        reg = next((r for r in historico_sat.listar() if r.get("id") == rec_id), None)
        movs = list((reg or {}).get("movs_extra") or []) + [extra]
        novo_log = list((reg or {}).get("log") or []) + log_ev
        historico_sat.atualizar(rec_id, {"movs_extra": movs, "log": novo_log})
    yield passo("Histórico atualizado", True,
                f"Mov {mv.get('mov_seq')} registrada como anexação complementar (sem tramitar/concluir)")
    yield {"done": True, "resultado": {"ok": True, "mov_seq": mv.get("mov_seq"), "n": len(anexos)}}


def mesclar_complementar_pendente(paj_norm: str, escopo: str = "assistido") -> dict:
    """Download complementar em PAJ AINDA NÃO ANEXADO: move os PDFs da subpasta
    'Complementar' para a pasta do escopo (assistido → principal; instituidor → 'Instituidor')
    e MESCLA o log/inventário no _log_download.json daquela pasta — upsert do inventário por
    (key, sub), salvos/pulados somados e eventos anexados com um marcador (log complementar
    visível no mesmo visualizador). SEM movimentação. Retorna {ok, movidos, n_arquivos}."""
    paths = _escopo_paths(paj_norm, escopo)
    comp = paths["complementar"]
    principal = paths["destino"]
    principal.mkdir(parents=True, exist_ok=True)
    movidos: list[str] = []
    if comp.is_dir():
        for f in sorted(comp.glob("*.pdf")):
            try:
                shutil.move(str(f), str(principal / f.name))
                movidos.append(f.name)
            except Exception:  # noqa: BLE001
                pass

    def _ler(p):
        try:
            return json.loads(p.read_text(encoding="utf-8")) if os.path.isfile(str(p)) else {}
        except Exception:  # noqa: BLE001
            return {}

    main = _ler(principal / "_log_download.json")
    cdata = _ler(comp / "_log_download.json")
    if cdata:
        salvos = list(main.get("salvos", []) or [])
        for s in cdata.get("salvos", []) or []:
            if s not in salvos:
                salvos.append(s)
        main["salvos"] = salvos
        # Merge do inventário SEM REBAIXAR: o download complementar marca os tipos NÃO
        # escolhidos como "nao_selecionado" — isso NÃO pode sobrescrever um "baixado"
        # anterior (senão os já baixados sumiriam do histórico). Prioridade: baixado > outros
        # status reais > nao_selecionado; e nunca introduzimos "nao_selecionado" como novidade.
        _PRIO = {"baixado": 3, "nao_selecionado": 0}
        _p = lambda st: _PRIO.get(st, 1)  # noqa: E731
        inv = list(main.get("inventario", []) or [])
        idx = {(it.get("key"), it.get("sub")): i for i, it in enumerate(inv)}
        for it in cdata.get("inventario", []) or []:
            chave = (it.get("key"), it.get("sub"))
            if chave in idx:
                if _p(it.get("status")) >= _p(inv[idx[chave]].get("status")):
                    inv[idx[chave]] = it   # só sobe/empata — nunca rebaixa um "baixado"
            elif it.get("status") != "nao_selecionado":
                inv.append(it); idx[chave] = len(inv) - 1  # ignora ruído de "não selecionado"
        main["inventario"] = inv
        main["pulados"] = list(main.get("pulados", []) or []) + list(cdata.get("pulados", []) or [])
        ev = list(main.get("eventos", []) or [])
        ev.append({"level": "novo",
                   "msg": f"── Download complementar (pendência) — "
                          f"{datetime.now():%d-%m-%Y %H:%M} ──"})
        ev.extend(cdata.get("eventos", []) or [])
        main["eventos"] = ev
        try:
            (principal / "_log_download.json").write_text(
                json.dumps(main, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    with __import__("contextlib").suppress(Exception):
        shutil.rmtree(comp, ignore_errors=True)
    return {"ok": True, "movidos": movidos, "n_arquivos": len(paths["listar"](paj_norm))}


def _gravar_hist(paj_norm, item, anexos, grupo, resultado, status, detalhe, log) -> None:
    if resultado.get("_dry"):  # nunca grava dry-run
        return
    if status == "dry_run":
        return
    historico_sat.registrar({
        "paj": _norm_to_num(paj_norm),
        "paj_norm": paj_norm,
        "id_processo": item.get("id", ""),
        "id_tramite": item.get("id_tramite", ""),
        "assistido": item.get("assistido", ""),
        "cpf": _cpf_do_paj(paj_norm),
        "n_arquivos": len(anexos),
        "arquivos": [a["nome"] for a in anexos],
        "mov_status": resultado.get("mov_status"),
        "mov_seq": resultado.get("mov_seq"),
        "mov_detalhe": resultado.get("mov_detalhe", ""),
        "tram_status": resultado.get("tram_status"),
        "tram_caixa": grupo,
        "tram_detalhe": resultado.get("tram_detalhe", ""),
        "concluido_status": resultado.get("concluido_status"),
        "concluido_detalhe": resultado.get("concluido_detalhe", ""),
        "status": status,
        "detalhe": detalhe,
        "log": log,
    })
