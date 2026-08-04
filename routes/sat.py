"""Rotas do Painel SAT Central — feature "Arquivos SAT".

- GET  /                             — página (tabela de PAJs manuais + histórico).
- GET  /configuracoes                — parametrização das consultas e da tramitação.
- POST /api/sat/pendentes/manual     — adiciona 1+ PAJs à lista (busca global no SISDPU).
- POST /api/sat/pendentes/manual/del — remove um PAJ manual da lista.
- GET  /api/sat/pendentes/manuais    — lista os PAJs cadastrados (única fonte —
                                       este painel não varre a caixa de entrada).
- GET  /api/sat/{paj_norm}/processar — SSE: movimenta (assistido+instituidor juntos)
                                       → tramita (query `grupo`, `prazo`, `dry`). Sem
                                       conclusão — o PAJ é manual, não há trâmite
                                       originário na caixa.
- GET  /api/sat/{paj_norm}/anexar-mais — SSE: anexação complementar (nova movimentação
                                       só de juntada) num PAJ já processado.
- GET  /api/sat/historico            — registros dos PAJs já trabalhados.
- GET  /api/sat/grupos               — caixas de grupo da unidade.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse
from starlette.background import BackgroundTask

from ingestao import sisdpu_movtram as movtram
from services import config_service as _cfg
from services import historico_sat, sat_service

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
PID_FILE = BASE_DIR / ".server.pid"


@router.get("/", response_class=HTMLResponse)
async def pagina_sat(request: Request):
    template = request.app.state.jinja.get_template("index.html")
    return HTMLResponse(template.render(request=request))


@router.get("/configuracoes", response_class=HTMLResponse)
async def pagina_config(request: Request):
    template = request.app.state.jinja.get_template("configuracoes.html")
    return HTMLResponse(template.render(request=request))


@router.get("/api/sat/pendentes/manuais", response_class=JSONResponse)
async def api_pendentes_manuais():
    """Únicos PAJs deste painel — cadastrados manualmente (sem varredura de caixa)."""
    return JSONResponse(sat_service.listar_pendentes_manuais())


@router.post("/api/sat/pendentes/manual", response_class=JSONResponse)
async def api_add_manual(request: Request):
    """Adiciona 1+ PAJs à lista, extraindo os dados AO VIVO do SISDPU.
    Body: {paj: "AAAA/UUU-NNNNN"} ou {pajs: [...]}. Retorna {ok, itens, erros}."""
    body = await request.json()
    entradas = body.get("pajs") or ([body.get("paj")] if body.get("paj") else [])
    itens, erros = [], []
    for txt in entradas:
        r = await sat_service.add_paj_manual(str(txt))
        if r.get("ok"):
            itens.append(r["item"])
        else:
            erros.append({"paj": str(txt), "erro": r.get("erro", "falha")})
    return JSONResponse({"ok": not erros, "itens": itens, "erros": erros})


@router.post("/api/sat/pendentes/manual/del", response_class=JSONResponse)
async def api_del_manual(request: Request):
    """Remove um PAJ manual da lista. Body: {paj_norm}."""
    body = await request.json()
    return JSONResponse(sat_service.del_paj_manual(body.get("paj_norm", "")))


@router.get("/api/sat/abrir", response_class=JSONResponse)
async def api_sat_abrir():
    """Abre a janela do SAT na tela para o login (usuário/senha/2FA/CAPTCHA)."""
    from ingestao import sat_client
    return JSONResponse(await sat_client.abrir())


@router.get("/api/sat/sessao", response_class=JSONResponse)
async def api_sat_sessao():
    """Verifica se a sessão do SAT está pronta (logado, na página de consulta)."""
    from ingestao import sat_client
    return JSONResponse(await sat_client.status())


@router.get("/api/sat/sessao-leve", response_class=JSONResponse)
async def api_sat_sessao_leve():
    """Status LEVE p/ polling automático: só lê a URL (não navega, não abre a janela)."""
    from ingestao import sat_client
    return JSONResponse(await sat_client.peek())


@router.get("/api/sat/historico", response_class=JSONResponse)
async def api_historico():
    regs = historico_sat.listar()
    # enriquece com a URL do detalhamento do PAJ no SISDPU (link do nº do PAJ),
    # construída a partir do id_processo — cobre registros antigos e novos.
    for r in regs:
        if not r.get("url_detalhe") and r.get("id_processo"):
            r["url_detalhe"] = (f"{movtram.SISDPU_URL}/pages/atendimento/"
                                f"detalhamentoProcesso.xhtml?id={r['id_processo']}")
    return JSONResponse({"registros": regs})


async def _stream_processar(paj_norm: str, grupo: str, dry: bool, prazo: int):
    async for ev in sat_service.processar_paj_stream(
        paj_norm, grupo=grupo or None, prazo_dias=prazo, dry_run=dry
    ):
        if ev.get("done"):
            yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
        else:
            yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}


def _parse_nbs(nbs: str) -> list[str]:
    """Divide a lista de NBs informada pelo usuário (separada por vírgula/espaço/;/quebra
    de linha) numa lista de strings; a normalização p/ dígitos é feita no sat_client."""
    import re as _re
    return [t for t in _re.split(r"[\s,;]+", (nbs or "").strip()) if t]


async def _stream_baixar(paj_norm: str, cpf: str, nome: str, delay: float,
                         bloqueio: float, pretensao: str, nbs: str = ""):
    from ingestao import sat_client
    pasta = str(sat_service.pasta_arquivos_sat(paj_norm))
    # resolve os tipos de documentos a baixar pela PRETENSÃO do PAJ (config por pretensão);
    # sem config → todos os tipos (comportamento padrão).
    try:
        tipos = _cfg.resolver_sat_tipos(pretensao, sat_client.TIPOS_KEYS)
    except Exception:  # noqa: BLE001
        tipos = None
    defaults = _cfg.get_sat_central_defaults()
    compilar, compilar_limiar = defaults["compilar"], defaults["compilar_limiar"]
    pat_ignorar = _cfg.get_sat_pat_ignorar()
    try:
        async for ev in sat_client.baixar_cidadao_stream(
                cpf, nome, pasta, delay_s=delay, bloqueio_s=bloqueio,
                tipos=tipos, pretensao=pretensao, nbs=_parse_nbs(nbs),
                compilar=compilar, compilar_limiar=compilar_limiar,
                pat_ignorar=pat_ignorar):
            if ev.get("done"):
                yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
            else:
                yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}
    except Exception as e:  # noqa: BLE001
        yield {"event": "log", "data": json.dumps({"level": "erro", "msg": str(e)}, ensure_ascii=False)}
        yield {"event": "done", "data": json.dumps({"ok": False, "detalhe": str(e)}, ensure_ascii=False)}


@router.get("/api/sat/{paj_norm}/arquivos", response_class=JSONResponse)
async def api_arquivos(paj_norm: str):
    arqs = sat_service.listar_todos_arquivos_sat(paj_norm)
    return JSONResponse({"arquivos": [
        {"nome": a["nome"], "tamanho": a.get("tamanho", 0), "excluido": a.get("excluido", False),
         "escopo": a.get("escopo", "assistido"), "referencia": a.get("referencia", "")}
        for a in arqs]})


@router.get("/api/sat/{paj_norm}/arquivo")
async def abrir_arquivo(paj_norm: str, nome: str):
    """Serve um PDF do SAT (ativo ou na lixeira 'Excluídos') INLINE, para abrir em nova
    aba do navegador. `nome` é o nome do arquivo (basename; sem travessia de caminho)."""
    base = Path(nome).name  # anti path-traversal
    # procura nas 4 pastas: assistido (principal + Excluídos) e instituidor (+ Excluídos)
    for pasta in (sat_service.pasta_arquivos_sat(paj_norm),
                  sat_service.pasta_instituidor(paj_norm),
                  sat_service.pasta_excluidos(paj_norm),
                  sat_service.pasta_excluidos_instituidor(paj_norm)):
        p = pasta / base
        if p.exists() and p.is_file():
            return FileResponse(
                str(p), media_type="application/pdf",
                headers={"Content-Disposition": f'inline; filename="{base}"'})
    return JSONResponse({"erro": "arquivo não encontrado"}, status_code=404)


@router.post("/api/sat/{paj_norm}/excluir-arquivo", response_class=JSONResponse)
async def api_excluir_arquivo(paj_norm: str, request: Request):
    """Move o documento para 'Excluídos' (soft delete). `escopo` = assistido|instituidor."""
    body = await request.json()
    return JSONResponse(sat_service.excluir_arquivo_sat(
        paj_norm, body.get("nome", ""), body.get("escopo", "assistido")))


@router.post("/api/sat/{paj_norm}/recuperar-arquivo", response_class=JSONResponse)
async def api_recuperar_arquivo(paj_norm: str, request: Request):
    """Traz o documento de volta de 'Excluídos' para a lista. `escopo` = assistido|instituidor."""
    body = await request.json()
    return JSONResponse(sat_service.recuperar_arquivo_sat(
        paj_norm, body.get("nome", ""), body.get("escopo", "assistido")))


@router.get("/api/sat/{paj_norm}/narrativa", response_class=JSONResponse)
async def api_narrativa(paj_norm: str):
    """Narrativa do PAJ (texto) para a linha expansível da coluna Pretensão."""
    return JSONResponse(sat_service.narrativa_paj(paj_norm))


@router.get("/api/sat/{paj_norm}/inventario", response_class=JSONResponse)
async def api_inventario(paj_norm: str):
    """Inventário dos documentos revelados no SAT (1º download) + marcas ja_baixado/
    ja_anexado + CPF — para o modal "Baixar mais". Inclui o catálogo p/ rótulos/grupos."""
    from ingestao import sat_client
    inv = sat_service.inventario_paj(paj_norm)
    inv["tipos"] = sat_client.TIPOS_DOCUMENTOS
    # havendo instituidor, oferece também o inventário dele (mesmo catálogo de tipos) para
    # que o modal "Baixar mais" permita baixar documentos complementares do instituidor.
    if sat_service.tem_instituidor(paj_norm):
        inv_i = sat_service.inventario_paj(paj_norm, escopo="instituidor")
        inv["instituidor"] = {"itens": inv_i.get("itens", []), "cpf": inv_i.get("cpf", "")}
    return JSONResponse(inv)


@router.get("/api/sat/{paj_norm}/log-download", response_class=JSONResponse)
async def log_download(paj_norm: str):
    """Log da última rotina de download (Baixar arquivos do SAT Central) do PAJ."""
    p = sat_service.pasta_arquivos_sat(paj_norm) / "_log_download.json"
    if not p.exists():
        return JSONResponse({"existe": False, "eventos": []})
    try:
        return JSONResponse({"existe": True, **json.loads(p.read_text(encoding="utf-8"))})
    except Exception:  # noqa: BLE001
        return JSONResponse({"existe": False, "eventos": []})


@router.get("/api/sat/{paj_norm}/baixar")
async def baixar(paj_norm: str, cpf: str, nome: str = "", delay: float = 5.0,
                 bloqueio: float = 45.0, pretensao: str = "", nbs: str = ""):
    """SSE do DOWNLOAD dos documentos do SAT (consulta + Grupos 1 e 2 + PAT). O usuário
    resolve o CAPTCHA na janela do SAT. Salva na pasta do PAJ."""
    return EventSourceResponse(_stream_baixar(paj_norm, cpf, nome, delay, bloqueio, pretensao, nbs))


async def _stream_baixar_instituidor(paj_norm: str, cpf: str, nome: str,
                                     delay: float, bloqueio: float):
    from ingestao import sat_client
    pasta = str(sat_service.pasta_instituidor(paj_norm))  # subpasta separada
    tipos = _cfg.get_sat_tipos_instituidor()
    if tipos is None:
        tipos = list(sat_client.TIPOS_INSTITUIDOR_DEFAULT)
    defaults = _cfg.get_sat_central_defaults()
    try:
        async for ev in sat_client.baixar_cidadao_stream(
                cpf, nome, pasta, delay_s=delay, bloqueio_s=bloqueio,
                tipos=tipos, pretensao="", rotulo="INSTITUIDOR - ",
                compilar=defaults["compilar"], compilar_limiar=defaults["compilar_limiar"]):
            if ev.get("done"):
                yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
            else:
                yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}
    except Exception as e:  # noqa: BLE001
        yield {"event": "log", "data": json.dumps({"level": "erro", "msg": str(e)}, ensure_ascii=False)}
        yield {"event": "done", "data": json.dumps({"ok": False, "detalhe": str(e)}, ensure_ascii=False)}


@router.get("/api/sat/{paj_norm}/baixar-instituidor")
async def baixar_instituidor(paj_norm: str, cpf: str, nome: str = "", delay: float = 5.0,
                             bloqueio: float = 45.0):
    """SSE do DOWNLOAD dos documentos do INSTITUIDOR (2ª consulta, CPF do instituidor;
    novo CAPTCHA na janela). Usado em pretensões derivadas (pensão por morte / auxílio-
    reclusão). Salva na MESMA pasta do PAJ com prefixo 'INSTITUIDOR - '."""
    return EventSourceResponse(_stream_baixar_instituidor(paj_norm, cpf, nome, delay, bloqueio))


@router.get("/api/sat/pat-ignorar", response_class=JSONResponse)
async def api_pat_ignorar_get():
    """Lista de espécies de processo administrativo (PAT) a ignorar (não baixar)."""
    return JSONResponse({"itens": _cfg.get_sat_pat_ignorar()})


@router.post("/api/sat/pat-ignorar", response_class=JSONResponse)
async def api_pat_ignorar_set(request: Request):
    """Salva a lista de espécies de PAT a ignorar."""
    body = await request.json()
    itens = body.get("itens") or []
    if not isinstance(itens, list):
        itens = []
    itens = _cfg.set_sat_pat_ignorar([str(x) for x in itens])
    return JSONResponse({"ok": True, "itens": itens})


@router.get("/api/sat/tipos", response_class=JSONResponse)
async def api_tipos_get():
    """Catálogo de tipos de documentos + seleção salva (padrão + por pretensão)."""
    from ingestao import sat_client
    return JSONResponse({
        "tipos": sat_client.TIPOS_DOCUMENTOS,
        "padrao": _cfg.get_sat_tipos_padrao(),  # None = todos
        "por_pretensao": _cfg.get_sat_tipos_por_pretensao(),
        "instituidor": _cfg.get_sat_tipos_instituidor(),  # None = usar o default abaixo
        "instituidor_default": sat_client.TIPOS_INSTITUIDOR_DEFAULT,
    })


@router.post("/api/sat/tipos", response_class=JSONResponse)
async def api_tipos_set(request: Request):
    """Salva as keys de uma pretensão (pretensão vazia = PADRÃO)."""
    body = await request.json()
    pretensao = str(body.get("pretensao", "") or "")
    keys = body.get("keys") or []
    if not isinstance(keys, list):
        keys = []
    res = _cfg.set_sat_tipos(pretensao, [str(k) for k in keys])
    return JSONResponse({"ok": True, **res})


@router.get("/api/sat/{paj_norm}/processar")
async def processar(paj_norm: str, grupo: str = "", dry: int = 0, prazo: int = 0):
    """SSE do processamento (movimenta → tramita; SEM conclusão — PAJ manual não tem
    trâmite originário na caixa). `dry=1` => simulação; `prazo` = dias de prazo de
    conclusão da tramitação (0 = sem prazo)."""
    return EventSourceResponse(_stream_processar(paj_norm, grupo, bool(dry), int(prazo)))


async def _stream_baixar_complementar(paj_norm: str, cpf: str, nome: str, delay: float,
                                      bloqueio: float, alvos_csv: str, escopo: str = "assistido"):
    import shutil
    from ingestao import sat_client
    paths = sat_service._escopo_paths(paj_norm, escopo)
    pasta = paths["complementar"]
    with __import__("contextlib").suppress(Exception):
        shutil.rmtree(pasta, ignore_errors=True)  # isola esta rodada
    alvos = [a for a in (alvos_csv or "").split(",") if a] or None  # "key|sub"
    tipos = sorted({a.split("|", 1)[0] for a in alvos}) if alvos else None
    # acervo = pasta do escopo (principal/instituidor): verificação prévia pula o que já foi
    # baixado (retomada de download interrompido, sem re-baixar os já presentes).
    acervo = str(paths["destino"])
    try:
        async for ev in sat_client.baixar_cidadao_stream(
                cpf, nome, str(pasta), delay_s=delay, bloqueio_s=bloqueio,
                tipos=tipos, pretensao="", rotulo=paths["rotulo"], alvos=alvos, pasta_acervo=acervo):
            if ev.get("done"):
                yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
            else:
                yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}
    except Exception as e:  # noqa: BLE001
        yield {"event": "log", "data": json.dumps({"level": "erro", "msg": str(e)}, ensure_ascii=False)}
        yield {"event": "done", "data": json.dumps({"ok": False, "detalhe": str(e)}, ensure_ascii=False)}


@router.get("/api/sat/{paj_norm}/baixar-complementar")
async def baixar_complementar(paj_norm: str, cpf: str, nome: str = "", delay: float = 5.0,
                              bloqueio: float = 45.0, alvos: str = "", escopo: str = "assistido"):
    """SSE do DOWNLOAD COMPLEMENTAR (nova consulta ao SAT no PAJ já concluído). `alvos` =
    "key|sub" separados por vírgula (subitens escolhidos). `escopo` = assistido|instituidor
    (instituidor usa CPF do instituidor, rótulo 'INSTITUIDOR - ' e subpasta 'Instituidor').
    Salva na subpasta complementar do escopo (limpa antes)."""
    return EventSourceResponse(
        _stream_baixar_complementar(paj_norm, cpf, nome, delay, bloqueio, alvos, escopo))


async def _stream_anexar_mais(paj_norm: str, rec: str, id_processo: str, assistido: str, nomes_csv: str):
    # filenames sanitizados não contêm '|' → separador seguro para a lista.
    nomes = [n for n in (nomes_csv or "").split("|") if n]
    async for ev in sat_service.anexar_mais_stream(paj_norm, rec, id_processo, assistido, nomes):
        if ev.get("done"):
            yield {"event": "done", "data": json.dumps(ev.get("resultado", {}), ensure_ascii=False)}
        else:
            yield {"event": "log", "data": json.dumps(ev, ensure_ascii=False)}


@router.get("/api/sat/{paj_norm}/anexar-mais")
async def anexar_mais(paj_norm: str, rec: str = "", id: str = "", assistido: str = "", nomes: str = ""):
    """SSE da ANEXAÇÃO COMPLEMENTAR (nova movimentação só de juntada, sem tramitar/concluir)
    num PAJ já processado. `rec` = id do registro do histórico a complementar; `nomes` =
    nomes dos arquivos escolhidos, separados por '|'."""
    return EventSourceResponse(_stream_anexar_mais(paj_norm, rec, id, assistido, nomes))


@router.post("/api/sat/{paj_norm}/mesclar-complementar", response_class=JSONResponse)
async def mesclar_complementar(paj_norm: str, escopo: str = "assistido"):
    """Download complementar em PAJ pendente (ainda NÃO anexado): move os PDFs para a pasta
    do escopo (assistido → principal; instituidor → 'Instituidor') e mescla o log/inventário
    — SEM movimentação. Os arquivos entram no acervo para a futura 'Anexar ao PAJ'."""
    return JSONResponse(sat_service.mesclar_complementar_pendente(paj_norm, escopo))


@router.get("/api/sat/grupos", response_class=JSONResponse)
async def api_grupos(id: str = ""):
    """Lista caixas de grupo da unidade. `id` = id_processo de referência p/ abrir a
    tela de tramitação; se ausente, usa o de um PAJ cadastrado."""
    id_processo = id
    if not id_processo:
        pend = sat_service.listar_pendentes_manuais()
        itens = pend.get("itens", []) if isinstance(pend, dict) else []
        id_processo = next((i.get("id_processo") for i in itens if i.get("id_processo")), "")
    grupos = await movtram.listar_grupos_unidade(id_processo) if id_processo else []
    return JSONResponse({"grupos": grupos, "padrao": sat_service.GRUPO_PADRAO})


# ---------------------------------------------------------------------------
# configurações — parametrização das consultas e da tramitação
# ---------------------------------------------------------------------------
@router.get("/api/config/sat-central", response_class=JSONResponse)
async def get_sat_central():
    return JSONResponse(_cfg.get_sat_central_defaults())


@router.post("/api/config/sat-central", response_class=JSONResponse)
async def set_sat_central(request: Request):
    body = await request.json()
    defaults = _cfg.set_sat_central_defaults(body)
    return JSONResponse({"ok": True, "defaults": defaults})


# ---------------------------------------------------------------------------
# segurança — status/reconexão da sessão do SISDPU (selo da barra superior)
# ---------------------------------------------------------------------------
@router.get("/api/seguranca/status", response_class=JSONResponse)
async def api_seguranca_status():
    from services import conexao_service
    return JSONResponse(conexao_service.status())


@router.post("/api/seguranca/reconectar", response_class=JSONResponse)
async def api_seguranca_reconectar():
    """Reconecta a sessão do SISDPU (recupera de conflito de sessão / sessão
    derrubada). Fecha o navegador headless e refaz o login com as credenciais do cofre."""
    from ingestao import sisdpu_client
    try:
        res = await sisdpu_client.reconectar()
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "status": "erro", "detalhe": str(e)}
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


# ---------------------------------------------------------------------------
# encerrar painel
# ---------------------------------------------------------------------------
def _encerrar_servidor() -> None:
    with __import__("contextlib").suppress(Exception):
        PID_FILE.unlink(missing_ok=True)
    time.sleep(0.4)
    os._exit(0)


@router.post("/api/encerrar", response_class=JSONResponse)
async def api_encerrar():
    return JSONResponse({"ok": True}, background=BackgroundTask(_encerrar_servidor))
