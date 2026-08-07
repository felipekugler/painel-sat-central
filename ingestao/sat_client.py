"""Cliente do SAT Central (INSS/Dataprev) via Playwright — janela VISÍVEL (headed) e
PERSISTENTE (processo separado, conectado por CDP).

Por que CDP e não `launch_persistent_context`: se o navegador fosse filho do processo
do painel, reiniciar o painel (comum ao atualizar `.py`) mataria o navegador e
derrubaria a sessão do SAT (cookies de sessão do GERID). Aqui o Chromium é lançado
como **processo independente** com `--remote-debugging-port`; o painel apenas
**conecta** via `connect_over_cdp`. Assim a sessão sobrevive a reinícios do painel — o
usuário loga uma vez e segue usando.

O login (usuário/senha/2FA) e o CAPTCHA são feitos pelo usuário NA JANELA.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

import config
from ingestao import sat_utils

_log = logging.getLogger("sat.client")

_PROFILE = Path(config.OFICIO_GERAL) / "_sat_profile"
SAT_CONSULTA = (
    "https://consultas.inss.gov.br/satcentral/pages/consultaCidadao/consultaCidadao.xhtml"
)
_CDP_PORT = 9222

_pw = None
_sat_pid: int | None = None  # PID do Chromium do SAT (p/ fechá-lo no shutdown do painel)


def _porta_aberta(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _pid_na_porta(port: int) -> int | None:
    """PID do processo LISTENING na porta (via netstat) — cobre o caso de o Chromium ter
    sobrevivido a um restart do painel (PID lançado desconhecido)."""
    with contextlib.suppress(Exception):
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                             capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            if f":{port} " in line and "LISTENING" in line.upper():
                return int(line.split()[-1])
    return None


def fechar_navegador() -> None:
    """Fecha o Chromium do SAT (chamado no SHUTDOWN do painel). Mata a árvore de processos
    pelo PID lançado ou, se desconhecido, pelo dono da porta CDP. Não roda em kill -9 do
    painel (só em encerramento gracioso), então reinícios de dev preservam a sessão."""
    global _sat_pid
    if not _porta_aberta(_CDP_PORT):
        _sat_pid = None
        return
    pid = _sat_pid or _pid_na_porta(_CDP_PORT)
    if pid:
        with contextlib.suppress(Exception):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10)
    _sat_pid = None


async def _pw_start():
    global _pw
    if _pw is None:
        _pw = await async_playwright().start()
    return _pw


async def _ensure_chromium() -> None:
    """Garante um Chromium persistente rodando com remote-debugging (processo separado).
    Lança-o apontando já para a consulta do SAT (o usuário loga na janela)."""
    global _sat_pid
    if _porta_aberta(_CDP_PORT):
        return
    pw = await _pw_start()
    exe = pw.chromium.executable_path
    _PROFILE.mkdir(parents=True, exist_ok=True)
    flags = 0
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP → sobrevive ao painel
        flags = 0x00000008 | 0x00000200
    proc = subprocess.Popen(
        [exe,
         f"--remote-debugging-port={_CDP_PORT}",
         f"--user-data-dir={_PROFILE}",
         "--no-first-run", "--no-default-browser-check",
         "--disable-features=PDFViewerUpdate,PdfUnseasoned",
         SAT_CONSULTA],
        creationflags=flags, close_fds=True,
    )
    _sat_pid = proc.pid  # p/ fechar o navegador no shutdown do painel
    for _ in range(60):
        if _porta_aberta(_CDP_PORT):
            time.sleep(1.0)  # respiro para o CDP subir de fato
            return
        time.sleep(0.5)


async def _connect():
    """Conecta ao Chromium persistente e devolve (browser, page). Chame `browser.close()`
    ao fim — fecha só a CONEXÃO CDP, não o Chromium."""
    await _ensure_chromium()
    pw = await _pw_start()
    browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{_CDP_PORT}")
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    return browser, page


def _status_dict(url: str) -> dict:
    u = (url or "").lower()
    logado = ("satcentral" in u) and ("cas/login" not in u) and ("geridinss" not in u)
    return {
        "logado": logado,
        "url": url,
        "msg": ("Sessão pronta no SAT — pode baixar." if logado
                else "Faça o login na janela do SAT (CPF, senha e código 2FA)."),
    }


async def abrir() -> dict:
    """Abre/traz à frente a janela do SAT e navega para a consulta (login se preciso)."""
    browser, page = await _connect()
    try:
        with contextlib.suppress(Exception):
            await page.bring_to_front()
        with contextlib.suppress(Exception):
            await page.goto(SAT_CONSULTA, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1500)
        return _status_dict(page.url)
    finally:
        with contextlib.suppress(Exception):
            await browser.close()


async def status() -> dict:
    """Verifica se a sessão está ativa navegando para a consulta (deixa a tela pronta)."""
    browser, page = await _connect()
    try:
        with contextlib.suppress(Exception):
            await page.goto(SAT_CONSULTA, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1200)
        return _status_dict(page.url)
    finally:
        with contextlib.suppress(Exception):
            await browser.close()


async def peek() -> dict:
    """Status LEVE p/ polling automático: só LÊ a URL das abas abertas — NÃO navega e
    NÃO abre o Chromium (se a janela estiver fechada, retorna 'não conectado'). Assim
    o selo atualiza sozinho sem recarregar a janela nem atrapalhar login/CAPTCHA."""
    if not _porta_aberta(_CDP_PORT):
        return {"logado": False, "url": "",
                "msg": "Janela do SAT fechada — clique em 'Abrir SAT / Login'."}
    try:
        pw = await _pw_start()
        browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{_CDP_PORT}")
    except Exception:  # noqa: BLE001
        return {"logado": False, "url": "", "msg": "Janela do SAT indisponível."}
    try:
        ctx = browser.contexts[0] if browser.contexts else None
        pages = list(ctx.pages) if ctx else []
        for p in pages:  # logado se ALGUMA aba está no satcentral (fora do login)
            d = _status_dict(p.url or "")
            if d["logado"]:
                return d
        return _status_dict(pages[0].url if pages else "")
    finally:
        with contextlib.suppress(Exception):
            await browser.close()


# ---------------------------------------------------------------------------
# Download dos documentos (Fase 2). Etapa 2a: consulta por CPF + Grupo 1.
# ---------------------------------------------------------------------------
# Anti-bloqueio: quando o SAT dá SILÊNCIO (nem abre PDF/modal, nem mostra aviso EXPRESSO
# de documento inexistente), é (quase) certeza de proteção ANTIROBÔ. Para os documentos
# que SEMPRE existem (flag `garantido`) o silêncio = antirobô com CERTEZA. Recuperação:
# pausar (45s FIXO, sem progressão — decisão do usuário 2026-07-21) e re-tentar SÓ o doc.
_MAX_TENT = 3
_PAUSA_BLOQUEIO_MS = 45000  # 45s fixos ao detectar bloqueio antirobô (era 30s)

# Ritmo entre consultas: delay APÓS cada consulta (campo "Tempo entre consultas", padrão
# 5s). É este ritmo (e não esperas fixas ad-hoc) que governa as transições entre docs/
# grupos/modais. Só há DOIS tempos no sistema: este e a espera após bloqueio antirobô.
_RITMO_MS = 5000

# Inventário ESTRUTURADO do download corrente: 1 item por documento REVELADO no SAT.
# item = {"key": <tipo>, "sub": <NB/protocolo/"">, "nome": <arquivo/"">, "status": ...}
# status: baixado · sem_dados · nao_relacionado · nao_consultado · antirobo · falha · descartado
_INVENTARIO: list[dict] = []

# Seleção por SUBITEM (download complementar): set de "key|sub" (ex.: "carta_concessao|703.969.453-1",
# "pat|1915479324"). None = sem restrição de subitem (baixa todas as linhas do tipo).
_ALVOS = None

# Filtro inicial por NB (Grupo 2 + PAT): set de NBs (SÓ DÍGITOS) indicado pelo usuário
# com base na narrativa. None/vazio = sem filtro (baixa tudo). Quando ativo, no Grupo 2
# e no PAT só se baixam docs do(s) NB(s) indicado(s). Grupo 1 e HISCRE (sem NB) NÃO são
# afetados. Setado no início de cada download.
_NB_FILTRO = None

# Filtro inicial por PROTOCOLO (só PAT): set de protocolos (SÓ DÍGITOS) indicado pelo
# usuário quando já se sabe qual tarefa do PAT interessa. None/vazio = sem filtro (baixa
# todas as tarefas). Não afeta Grupo 1/Grupo 2 (protocolo é conceito só do PAT). Setado no
# início de cada download.
_PROTOCOLO_FILTRO = None

# "Baixar exclusivamente processo administrativo" (opção GLOBAL, qualquer consulta, com ou
# sem _PROTOCOLO_FILTRO): quando True, pula Grupo 1 e Grupo 2 por completo e vai direto ao
# PAT — só afeta a mensagem de log (o skip em si já acontece via `tipos=["pat"]"`, que a
# rota monta quando esta opção é marcada). Setado no início de cada download.
_SOMENTE_PAT = False

# Compilação de PDFs por tipo (CCON/CRER/Laudo Médico/Laudo Social): quando o nº de docs
# de um tipo passa de _COMPILAR_LIMIAR, une num único PDF (ordem de NB decrescente) para
# reduzir os anexos da movimentação. Setado no início de cada download (só o principal).
_COMPILAR = False
_COMPILAR_LIMIAR = 3

# Espécies de processo administrativo (PAT) a IGNORAR — nomes de "Serviço" irrelevantes
# (config, editável na aba). Comparação por substring normalizada. O PAT cujo Serviço bate
# NÃO é baixado (skip ANTES do clique). Setado no início de cada download principal.
_PAT_IGNORAR: list[str] = []

# Pasta do ACERVO (destino final dos PDFs) usada para VERIFICAÇÃO PRÉVIA: se o documento
# já existe lá, pula o download (evita re-baixar/sobrescrever e permite RETOMAR um download
# interrompido no meio de uma categoria). Setado no início de cada download.
_PASTA_ACERVO: str | None = None

# Callback de PERSISTÊNCIA INCREMENTAL do log: gravado a cada documento (não só no fim),
# para que uma interrupção (perda de conexão) sempre deixe o log/inventário parcial em disco.
# Padrão do painel: todo log de rotina longa deve ser gravado incrementalmente.
_LOG_PERSIST = None


def _pat_ignorado(servico: str) -> bool:
    """True se o Serviço do PAT casa (substring normalizada) alguma espécie da lista de
    ignorados configurada — nesse caso a tarefa NÃO é baixada."""
    s = _norm(servico)
    return bool(s) and any(_norm(t) in s for t in _PAT_IGNORAR if str(t).strip())


def _ja_no_acervo(nome_arq: str) -> bool:
    """True se o arquivo (nome final) já existe no acervo — verificação prévia p/ pular
    o download e permitir retomar de onde parou."""
    if not _PASTA_ACERVO or not nome_arq:
        return False
    with contextlib.suppress(Exception):
        return (Path(_PASTA_ACERVO) / nome_arq).exists()
    return False


def _pat_ja_no_acervo(prot: str) -> bool:
    """True se já existe um PROCADM do protocolo `prot` no acervo (o nome termina em
    'PROT <prot>.pdf'). Verificação prévia p/ pular tarefas PAT já baixadas."""
    if not _PASTA_ACERVO or not prot:
        return False
    with contextlib.suppress(Exception):
        return any(Path(_PASTA_ACERVO).glob(f"*PROT {prot}.pdf"))
    return False


def _inv_add(key: str, sub: str, nome: str, status: str, rotulo: str = "") -> None:
    # `rotulo` = rótulo humano do item (ex.: o Serviço do PAT), para exibir no "Baixar mais".
    _INVENTARIO.append({"key": key, "sub": (sub or ""), "nome": (nome or ""),
                        "status": status, "rotulo": (rotulo or "")})
    # Persistência incremental: grava o log a cada documento (não só no fim).
    if _LOG_PERSIST is not None:
        with contextlib.suppress(Exception):
            _LOG_PERSIST()


async def _ritmo(page) -> None:
    """Aplica o ritmo entre consultas: delay APÓS cada consulta (Tempo entre consultas)."""
    if _RITMO_MS > 0:
        await page.wait_for_timeout(_RITMO_MS)


# Catálogo dos tipos de documentos que a rotina sabe baixar (key estável, rótulo, grupo).
# A seleção POR PRETENSÃO (config) filtra o download por essas keys.
_G1 = "Grupo 1 — CNIS / Declarações (1 clique)"
_G2 = "Grupo 2 — modais"
_GP = "PAT"
TIPOS_DOCUMENTOS = [
    {"key": "cnis_atividades", "label": "CNIS - Atividades", "grupo": _G1},
    {"key": "cnis_dados_cadastrais", "label": "CNIS - Dados Cadastrais", "grupo": _G1},
    {"key": "cnis_elos", "label": "CNIS - Elos", "grupo": _G1},
    {"key": "cnis_microfichas", "label": "CNIS - Microfichas", "grupo": _G1},
    {"key": "cnis_remuneracoes", "label": "CNIS - Remunerações", "grupo": _G1},
    {"key": "cnis_vinculos", "label": "CNIS - Vínculos", "grupo": _G1},
    {"key": "declaracao_beneficios", "label": "Declaração de Benefícios", "grupo": _G1},
    {"key": "requerimentos_sibe", "label": "Requerimentos do SIBE", "grupo": _G1},
    {"key": "revisao_art29", "label": "Revisão de Benefício - Artigo 29", "grupo": _G1},
    {"key": "carta_concessao", "label": "Carta de Concessão", "grupo": _G2},
    {"key": "crer", "label": "Comunicação de Resultado de Requerimento (CRER)", "grupo": _G2},
    {"key": "hiscre", "label": "Histórico de Créditos (HISCRE)", "grupo": _G2},
    {"key": "consignados", "label": "Histórico de Empréstimos Consignados", "grupo": _G2},
    {"key": "laudo_social", "label": "Laudo de Avaliação Social", "grupo": _G2},
    {"key": "laudo_medico", "label": "Laudo Médico", "grupo": _G2},
    {"key": "pat", "label": "Tarefas PAT (2ª Via dos Processos)", "grupo": _GP},
]
TIPOS_KEYS = [t["key"] for t in TIPOS_DOCUMENTOS]

# Seleção-padrão dos docs do INSTITUIDOR (benefícios derivados) — foco em provar a
# qualidade de segurado dele. Editável na UI ("Escolher" → item "Instituidor").
TIPOS_INSTITUIDOR_DEFAULT = [
    "cnis_dados_cadastrais", "cnis_atividades", "cnis_vinculos", "cnis_remuneracoes",
    "declaracao_beneficios", "carta_concessao", "crer",
]

# Prefixo aplicado aos nomes dos arquivos (ex.: "INSTITUIDOR - " na 2ª consulta).
# Definido no início de cada download.
_ROTULO_ARQ = ""


def _final_nome(base: str) -> str:
    """Nome final do PDF (com o prefixo/rótulo do download corrente)."""
    return sat_utils.nome_final(_ROTULO_ARQ + base) + ".pdf"


# Grupo 1 = abrem direto com 1 clique em "Consultar". (rótulo, prefixo, GARANTIDO, key).
# `garantido`=True → o doc SEMPRE existe; silêncio ao consultar = antirobô com certeza.
_GRUPO1 = [
    ("CNIS - Atividades do CNIS v.3", "CNIS - Atividades", False, "cnis_atividades"),
    ("CNIS - Dados Cadastrais do CNIS", "CNIS - Dados Cadastrais", True, "cnis_dados_cadastrais"),
    ("CNIS - Elos do CNIS", "CNIS - Elos", False, "cnis_elos"),
    ("CNIS - Microfichas do CNIS v.3", "CNIS - Microfichas", False, "cnis_microfichas"),
    ("CNIS - Remunerações do CNIS v.3", "CNIS - Remuneracoes", True, "cnis_remuneracoes"),
    ("CNIS - Vínculos do CNIS v.3", "CNIS - Vinculos", True, "cnis_vinculos"),
    ("Declaração de Benefícios", "Declaracao de Beneficios", True, "declaracao_beneficios"),
    ("Requerimentos do SIBE", "Requerimentos do SIBE", False, "requerimentos_sibe"),
    ("Revisão de Benefício - Artigo 29", "Revisao de Beneficio - Artigo 29", False, "revisao_art29"),
]

# Clique no "Consultar" pelo rótulo (comparação NORMALIZADA — sem acento, espaços
# colapsados, minúsculo — o innerText do td tem espaçamento/acentos variáveis).
_JS_CLK = r"""(label)=>{
  const norm=s=>(s||'').normalize('NFD').replace(/[̀-ͯ]/g,'').replace(/\s+/g,' ').trim().toLowerCase();
  const alvo=norm(label);
  const btns=[...document.querySelectorAll('button[type=submit]')].filter(b=>norm(b.innerText||b.value)==='consultar');
  const btn=btns.find(b=>{const tr=b.closest('tr'); const td=tr&&tr.querySelector('td'); return td&&norm(td.innerText)===alvo;});
  if(btn){btn.click(); return true;} return false;
}"""

_JS_BANNER = r"""()=>{
  const m=[...document.querySelectorAll('.ui-messages .ui-message, .ui-messages-warn, .ui-messages-error')]
    .map(e=>(e.innerText||'').trim()).filter(Boolean);
  return m.join(' | ');
}"""

# Marcação de banners para detectar mensagem NOVA (não residual). Guarda o texto atual
# de cada mensagem em data-sat-seen; uma mensagem é "nova" quando o nó ainda não foi
# marcado OU seu texto mudou desde a marcação (o PrimeFaces re-renderiza .ui-messages a
# cada AJAX, criando nós novos → funciona mesmo p/ textos idênticos consecutivos).
_JS_MARCAR_BANNERS = r"""()=>{
  document.querySelectorAll('.ui-messages .ui-message, .ui-messages-warn, .ui-messages-error')
    .forEach(e=>{ e.dataset.satSeen=(e.innerText||'').trim(); });
}"""

_JS_BANNER_NOVO = r"""()=>{
  const els=[...document.querySelectorAll('.ui-messages .ui-message, .ui-messages-warn, .ui-messages-error')];
  const novas=[];
  for(const e of els){ const t=(e.innerText||'').trim(); if(t && e.dataset.satSeen!==t){ novas.push(t); } e.dataset.satSeen=t; }
  return novas.join(' | ');
}"""

# Frases do SAT que indicam AUSÊNCIA de documento/benefício (o doc não existe) — NÃO é
# bloqueio antirobô. Ex.: "não retornou dados", "Não foram encontrados benefícios...".
_MARCAS_VAZIO = ("nao retorn", "nao foram encontrados", "nao encontrad",
                 "nenhum beneficio", "nenhum registro", "sem beneficio")


def _banner_vazio(banner_norm: str) -> bool:
    return any(m in banner_norm for m in _MARCAS_VAZIO)


def _classificar_banner(banner_norm: str):
    """Classifica um banner NOVO (não residual). Retorna:
      None          → silêncio (nenhuma mensagem) ⇒ candidato a antirobô
      'sem_dados'   → mensagem expressa de ausência (doc não existe)
      'indisponivel'→ QUALQUER outra mensagem expressa (indisponível/erro/outro motivo)
    O antirobô SÓ é acionado quando NÃO há mensagem alguma (retorno None)."""
    if not banner_norm:
        return None
    return "sem_dados" if _banner_vazio(banner_norm) else "indisponivel"

# Painel de Consultas RENDERIZADO: >=5 botões "Consultar" cujo td (rótulo) já tem texto.
_JS_TEM_CONSULTAR = r"""()=>{
  const norm=s=>(s||'').normalize('NFD').replace(/[̀-ͯ]/g,'').replace(/\s+/g,' ').trim().toLowerCase();
  const n=[...document.querySelectorAll('button[type=submit]')]
    .filter(b=>norm(b.innerText||b.value)==='consultar')
    .filter(b=>{const tr=b.closest('tr'); const td=tr&&tr.querySelector('td'); return td && td.innerText.trim().length>3;}).length;
  return n>=5;
}"""


def _pdf_url(aba_url: str) -> str:
    """URL http real do PDF: o visualizador (Adobe/Chrome) traz a URL em `pdfurl=`."""
    from urllib.parse import urlparse, parse_qs, unquote
    if "pdfurl=" in (aba_url or ""):
        v = parse_qs(urlparse(aba_url).query).get("pdfurl", [""])[0]
        if v:
            return unquote(v)
    if (aba_url or "").startswith("http"):
        return aba_url
    return "https://consultas.inss.gov.br/satcentral/downloads"


async def _baixar_pdf(ctx, url: str, caminho: str) -> bool:
    """Baixa os bytes do PDF exibido na aba /downloads (usa os cookies da sessão)."""
    try:
        resp = await ctx.request.get(url, timeout=60000)
        body = await resp.body()
        if body[:5] == b"%PDF-" and len(body) > 500:
            Path(caminho).write_bytes(body)
            return True
        _log.warning("sat pdf inválido (%s bytes) em %s", len(body), url)
    except Exception as e:  # noqa: BLE001
        _log.warning("sat baixar pdf %s: %r", url, e)
    return False


def _ev(msg: str, level: str = "info") -> dict:
    return {"level": level, "msg": msg}


def _norm(s: str) -> str:
    import re as _re
    import unicodedata as _u
    s = "".join(c for c in _u.normalize("NFD", s or "") if _u.category(c) != "Mn")
    return _re.sub(r"\s+", " ", s).strip().lower()


# ---------------------------------------------------------------------------
# Grupo 2 (documentos que abrem em MODAL) e PAT. Portados da skill
# SAT-DownloadArquivos (helpers dlg/linhasModal/abrirPdfLinha/btnModal/campoModal).
# ---------------------------------------------------------------------------
# O SAT pré-carrega ~14 modais PrimeFaces na mesma página, quase todos INVISÍVEIS
# até clicar o "Consultar" correspondente. SEMPRE operar só no dialog VISÍVEL
# (display/visibility ok, opacity>0, rect>0) e ler as linhas ACHATADAS
# (cells.join(' | ')) — a extração aninhada mascara o NB como "[BLOCKED: JWT token]".
_JS_DLG_FRAG = r"""
const _norm=s=>(s||'').normalize('NFD').replace(/[̀-ͯ]/g,'').replace(/\s+/g,' ').trim().toLowerCase();
const _dlg=()=>{const vis=[...document.querySelectorAll('.ui-dialog')].filter(d=>{const s=getComputedStyle(d);const r=d.getBoundingClientRect();return s.display!=='none'&&s.visibility!=='hidden'&&parseFloat(s.opacity||'1')>0&&r.width>5&&r.height>5;});return vis.length?vis[vis.length-1]:null;};
const _dlgc=()=>{const d=_dlg();return d?(d.querySelector('.ui-dialog-content')||d):null;};
const _linhas=()=>{const c=_dlgc();if(!c)return[];return [...c.querySelectorAll('tbody tr')].filter(tr=>tr.querySelector('td')).map(tr=>[...tr.querySelectorAll('td')].map(td=>(td.innerText||'').replace(/\s+/g,' ').trim()).join(' | '));};
"""

# Consultar por rótulo, tolerante: exato e (fallback) "contém".
_JS_CLK_CONTEM = r"""(label)=>{
  const norm=s=>(s||'').normalize('NFD').replace(/[̀-ͯ]/g,'').replace(/\s+/g,' ').trim().toLowerCase();
  const alvo=norm(label);
  const btns=[...document.querySelectorAll('button[type=submit]')].filter(b=>norm(b.innerText||b.value)==='consultar');
  const btn=btns.find(b=>{const tr=b.closest('tr'); const td=tr&&tr.querySelector('td'); return td&&norm(td.innerText).includes(alvo);});
  if(btn){btn.click(); return true;} return false;
}"""

_JS_DIALOG_VIS = "()=>{" + _JS_DLG_FRAG + " return !!_dlg();}"
_JS_LINHAS_MODAL = "()=>{" + _JS_DLG_FRAG + " return _linhas();}"

# Clica o "Abrir PDF" da linha n do dialog visível (mesmo índice de _linhas).
_JS_ABRIR_PDF_LINHA = "(n)=>{" + _JS_DLG_FRAG + r"""
  const c=_dlgc(); if(!c) return false;
  const trs=[...c.querySelectorAll('tbody tr')].filter(tr=>tr.querySelector('td'));
  const tr=trs[n]; if(!tr) return false;
  let b=[...tr.querySelectorAll('button,a,input[type=button],input[type=submit]')]
    .find(x=>_norm((x.innerText||'')+' '+(x.value||'')+' '+(x.title||'')).includes('abrir pdf'));
  if(!b) b=tr.querySelector('button,a,input[type=button],input[type=submit]');
  if(b){b.click(); return true;} return false;
}"""

# Clica um botão do dialog visível cujo texto casa a regex (OK/Consultar/Confirmar…).
_JS_BTN_MODAL = "(re)=>{" + _JS_DLG_FRAG + r"""
  const d=_dlg(); if(!d) return false;
  const rx=new RegExp(re,'i');
  const b=[...d.querySelectorAll('button,a,input[type=button],input[type=submit]')]
    .filter(x=>x.offsetParent!==null)
    .find(x=>rx.test(_norm((x.innerText||'')+' '+(x.value||''))));
  if(b){b.click(); return true;} return false;
}"""

# Preenche o input de texto de índice idx do dialog visível (HISCRE: 0=De, 1=até).
_JS_CAMPO_MODAL = "(a)=>{" + _JS_DLG_FRAG + r"""
  const d=_dlg(); if(!d) return false;
  const idx=a[0], valor=a[1];
  const ins=[...d.querySelectorAll('input')].filter(i=>i.offsetParent!==null
     && (i.type==='text'||i.type===''||i.type==null) && !i.readOnly && !i.disabled);
  const el=ins[idx]; if(!el) return false;
  el.focus(); el.value=valor;
  el.dispatchEvent(new Event('input',{bubbles:true}));
  el.dispatchEvent(new Event('change',{bubbles:true}));
  el.dispatchEvent(new KeyboardEvent('keyup',{bubbles:true}));
  return true;
}"""

# Fecha o dialog visível pelo "x" da barra de título.
_JS_FECHAR_MODAL = "()=>{" + _JS_DLG_FRAG + r"""
  const d=_dlg(); if(!d) return false;
  const x=d.querySelector('.ui-dialog-titlebar-close');
  if(x){x.click(); return true;} return false;
}"""

# Avança uma página no paginador PrimeFaces do dialog visível (PAT).
_JS_PAG_PROX = "()=>{" + _JS_DLG_FRAG + r"""
  const d=_dlg(); if(!d) return false;
  const nx=d.querySelector('.ui-paginator-next');
  if(nx && !nx.classList.contains('ui-state-disabled')){nx.click(); return true;}
  return false;
}"""


# Situações conhecidas (para deduzir a coluna "Situação" das linhas achatadas).
_SIT_TOKENS = ("ativo", "cessado", "indeferido", "deferido", "concedido", "suspenso",
               "cancelado", "ativa", "cessada", "indeferida", "deferida",
               "em manutencao", "emitido", "concluido")


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.split("|") if c.strip()]


def _cells_dados(row: str) -> list[str]:
    """Células de dados, sem a célula do botão 'Abrir PDF' (última coluna das tabelas)."""
    return [c for c in _cells(row) if _norm(c) != "abrir pdf"]


def _achar_nb_row(row: str) -> str:
    m = re.search(r"\d{3}\.\d{3}\.\d{3}-\d(?!\d)", row)
    if m:
        return m.group(0)
    m = re.search(r"(?<!\d)(\d{10})(?!\d)", row)
    if m:
        r = m.group(1)
        return f"{r[0:3]}.{r[3:6]}.{r[6:9]}-{r[9]}"
    return ""


def _achar_situacao(row: str) -> str:
    n = _norm(row)
    for t in _SIT_TOKENS:
        if t in n:
            return t.upper()
    cells = _cells_dados(row)
    return cells[-1].upper() if cells else ""


def _achar_especie(row: str) -> str:
    """Célula alfabética mais longa que não seja NB/data/situação (usada no CRER)."""
    cand = []
    for c in _cells_dados(row):
        if not any(ch.isalpha() for ch in c):
            continue
        if re.search(r"\d{3}\.\d{3}", c) or re.fullmatch(r"[\d/.\- ]+", c):
            continue
        if _norm(c) in _SIT_TOKENS:
            continue
        cand.append(c)
    return max(cand, key=len) if cand else ""


async def _baixar_aba(ctx, nova, caminho: str) -> bool:
    """Carrega a aba do visualizador, baixa os bytes do PDF e fecha a aba."""
    with contextlib.suppress(Exception):
        await nova.wait_for_load_state("domcontentloaded", timeout=15000)
    ok = await _baixar_pdf(ctx, _pdf_url(nova.url), caminho)
    with contextlib.suppress(Exception):
        await nova.close()
    return ok


async def _clicar_abre_aba(page, ctx, fn_click, out, reg, nome_doc="documento",
                           alvo_norm="", garantido=False):
    """Executa fn_click() (async → bool de clicou) e aguarda um dos 3 desfechos:
    'aba' (abriu o PDF), 'sem_dados' (banner que MENCIONA o documento) ou 'bloqueio'
    (SILÊNCIO = antirobô → pausa e re-tenta). Gerador: yield-a os avisos ao vivo e
    grava o resultado em out['nova']/out['desfecho']."""
    nova, desfecho, msg = None, "bloqueio", ""
    antes_ini = set(ctx.pages)
    for tentativa in range(1, _MAX_TENT + 1):
        antes = set(ctx.pages)
        await page.evaluate(_JS_MARCAR_BANNERS)  # marca banners RESIDUAIS como vistos
        clicou = await fn_click()
        if clicou:
            for _ in range(16):  # ~8s
                await page.wait_for_timeout(500)
                novas = [p for p in ctx.pages if p not in antes]
                if novas:  # abriu o PDF numa aba nova → prioridade sobre banner
                    nova, desfecho = novas[0], "aba"
                    break
                # QUALQUER mensagem NOVA (não residual) = motivo EXPRESSO → NÃO é antirobô.
                # 'sem_dados' (ausência) ou 'indisponivel' (outro motivo). O antirobô só
                # dispara no SILÊNCIO absoluto (nenhuma mensagem).
                banner = _norm(await page.evaluate(_JS_BANNER_NOVO))
                cls = _classificar_banner(banner)
                if cls:
                    desfecho, msg = cls, banner
                    break
            if desfecho in ("aba", "sem_dados", "indisponivel"):
                break
        # SILÊNCIO (nem aba nem mensagem) → proteção antirobô ativada
        if tentativa < _MAX_TENT:
            cert = "" if garantido else " (provável)"
            yield reg(f"  🛡 proteção antirobô{cert} ativada em {nome_doc} — aguardando "
                      f"{_PAUSA_BLOQUEIO_MS // 1000}s e re-tentando "
                      f"({tentativa}/{_MAX_TENT - 1})", "muted")
            await page.wait_for_timeout(_PAUSA_BLOQUEIO_MS)
    # fecha abas órfãs (algum PDF que abriu mas não virou desfecho 'aba')
    if desfecho != "aba":
        for p in list(ctx.pages):
            if p not in antes_ini:
                with contextlib.suppress(Exception):
                    await p.close()
    out["nova"], out["desfecho"], out["msg"] = nova, desfecho, msg
    await _ritmo(page)  # ritmo: delay entre consultas


async def _sem_modal(page) -> bool:
    return not await page.evaluate(_JS_DIALOG_VIS)


async def _fechar_modal(page) -> None:
    with contextlib.suppress(Exception):
        await page.evaluate(_JS_FECHAR_MODAL)
    for _ in range(8):  # aguarda o fade-out (opacity) sumir
        await page.wait_for_timeout(300)
        if await _sem_modal(page):
            return


async def _abrir_modal(page, label, out, reg, nome_doc="documento",
                       alvo_norm="", garantido=False):
    """Clica o 'Consultar' do rótulo e espera abrir o MODAL. Gerador: yield-a avisos ao
    vivo e grava out['estado'] = 'modal' | 'sem_dados' | 'indisponivel' | 'bloqueio'
    e out['msg'] com o texto do aviso capturado (quando houver)."""
    # garante que nenhum modal antigo esteja visível antes de clicar
    for _ in range(6):
        if await _sem_modal(page):
            break
        await _fechar_modal(page)
    out["estado"] = "bloqueio"
    out["msg"] = ""
    for tentativa in range(1, _MAX_TENT + 1):
        await page.evaluate(_JS_MARCAR_BANNERS)  # marca banners RESIDUAIS como vistos
        clicou = await page.evaluate(_JS_CLK, label)
        if not clicou:
            clicou = await page.evaluate(_JS_CLK_CONTEM, label)
        if clicou:
            for _ in range(20):  # ~8s
                await page.wait_for_timeout(400)
                if await page.evaluate(_JS_DIALOG_VIS):
                    out["estado"] = "modal"
                    await _ritmo(page)  # ritmo após abrir o modal (é uma consulta)
                    return
                # Qualquer aviso EXPRESSO NOVO (não residual) encerra a espera SEM disparar
                # antirobô: "sem_dados" (ausência de documento/benefício) ou "indisponivel"
                # (documento inacessível por outro motivo). Só o SILÊNCIO total é antirobô.
                banner = _norm(await page.evaluate(_JS_BANNER_NOVO))
                cls = _classificar_banner(banner)
                if cls:
                    out["estado"], out["msg"] = cls, banner
                    await _ritmo(page)
                    return
        # SILÊNCIO (nem modal nem aviso expresso) → proteção antirobô
        if tentativa < _MAX_TENT:
            cert = "" if garantido else " (provável)"
            yield reg(f"  🛡 proteção antirobô{cert} ativada em {nome_doc} — aguardando "
                      f"{_PAUSA_BLOQUEIO_MS // 1000}s e re-tentando "
                      f"({tentativa}/{_MAX_TENT - 1})", "muted")
            await page.wait_for_timeout(_PAUSA_BLOQUEIO_MS)


# Grupo 2 = abrem em MODAL. tipo "tabela": linhas com "Abrir PDF"; "hiscre": form de datas.
_GRUPO2 = [
    {"key": "carta_concessao", "label": "Carta de Concessão", "tipo": "tabela",
     "nome": "CCON", "titulo": "carta", "filtra_especie": True},
    {"key": "crer", "label": "Comunicação de Resultado de Requerimento - CRER",
     "tipo": "tabela", "nome": "CRER", "titulo": "crer", "especie": True, "filtra_especie": True},
    {"key": "hiscre", "label": "Histórico de Créditos", "tipo": "hiscre",
     "nome": "HISCRE", "titulo": "credito", "garantido": True},
    {"key": "consignados", "label": "Histórico de Empréstimos Consignados",
     "tipo": "tabela", "nome": "HISCONSIGNADOS", "titulo": "emprestimo"},
    {"key": "laudo_social", "label": "Laudo de Avaliação Social", "tipo": "tabela",
     "nome": "LAUDO SOCIAL", "titulo": "avaliacao social"},
    {"key": "laudo_medico", "label": "Laudo Médico", "tipo": "tabela",
     "nome": "LAUDO MEDICO", "titulo": "laudo"},
]


# --- Relação espécie × pretensão (filtro automático de CCON/CRER) ---
# Pretensão (pretensaopura, normalizada) → palavras-chave que devem aparecer na espécie.
# Casamento por SUBSTRING (tolera variações). Pretensão AUSENTE do mapa OU DERIVADA ⇒
# SEM restrição (baixa todas) — evita excluir documento relevante por engano.
_PRETENSAO_ESPECIES = {
    "beneficio de prestacao continuada": ["prestacao continuada", "assistencial"],
    "beneficio assistencial": ["prestacao continuada", "assistencial"],
    "bpc": ["prestacao continuada", "assistencial"],
    "aposentadoria por idade": ["aposentadoria por idade"],
    "aposentadoria por tempo de contribuicao": ["tempo de contribuicao"],
    "aposentadoria por incapacidade permanente": ["incapacidade permanente", "invalidez"],
    "aposentadoria por invalidez": ["incapacidade permanente", "invalidez"],
    "auxilio por incapacidade temporaria": ["incapacidade temporaria", "auxilio-doenca", "auxilio doenca"],
    "auxilio-doenca": ["incapacidade temporaria", "auxilio-doenca", "auxilio doenca"],
    "auxilio doenca": ["incapacidade temporaria", "auxilio-doenca", "auxilio doenca"],
    "auxilio-acidente": ["auxilio-acidente", "auxilio acidente"],
    "auxilio acidente": ["auxilio-acidente", "auxilio acidente"],
    "salario-maternidade": ["maternidade"],
    "salario maternidade": ["maternidade"],
    # Derivados: no SAT do assistido, o doc relevante é o do PRÓPRIO benefício derivado
    # (a qualidade de segurado do instituidor é baixada à parte, com o CPF dele).
    "pensao por morte": ["pensao por morte"],
    "auxilio-reclusao": ["reclusao"],
    "auxilio reclusao": ["reclusao"],
}


def especie_relacionada(pretensao: str, especie: str) -> bool:
    """True se a espécie do benefício se relaciona à pretensão do PAJ. SEGURO: pretensão
    vazia/não mapeada → True (não restringe)."""
    p = _norm(pretensao)
    e = _norm(especie)
    if not p or not e:
        return True
    kws = None
    for chave, lista in _PRETENSAO_ESPECIES.items():
        if chave in p or p in chave:
            kws = lista
            break
    if kws is None:
        return True  # pretensão não mapeada → não restringe
    return any(k in e for k in kws)


async def _baixar_modal_tabela(page, ctx, cfg, nome, pasta_destino, salvos, pulados, reg,
                               pretensao=""):
    """Modais-tabela (CCON/CRER/Consignados/Laudo): 'Abrir PDF' por linha de benefício.
    Para os cfgs com `filtra_especie`, pula (e LOGA) linhas cuja espécie não se relaciona
    à `pretensao` do PAJ."""
    linhas = await page.evaluate(_JS_LINHAS_MODAL)
    idxs = [i for i, l in enumerate(linhas) if _achar_nb_row(l)]
    if not idxs:
        pulados.append(f"{cfg['nome']} (sem linhas)")
        _inv_add(cfg["key"], "", "", "sem_dados")
        yield reg(f"  – {cfg['label']}: sem benefícios na tabela", "muted")
        return
    alvo = _norm(cfg.get("titulo") or cfg["label"])
    for i in idxs:
        row = linhas[i]
        nb = _achar_nb_row(row)
        sit = _achar_situacao(row)
        # download por SUBITEM (complementar): baixar só os NBs escolhidos
        # (ou o tipo inteiro, se veio o curinga "key|").
        if (_ALVOS is not None and f"{cfg['key']}|{nb}" not in _ALVOS
                and f"{cfg['key']}|" not in _ALVOS):
            continue
        # Filtro inicial por NB (indicado pelo usuário na narrativa): pula linhas de NB
        # não indicado — REGISTRA no log. Vale p/ TODOS os modais-tabela (incl. Consignados).
        nb_indicado = _NB_FILTRO is not None and sat_utils.nb_digits(nb) in _NB_FILTRO
        if _NB_FILTRO is not None and not nb_indicado:
            pulados.append(f"{cfg['nome']} {nb} — pulado (NB não indicado pelo usuário)")
            _inv_add(cfg["key"], nb, "", "nao_filtrado_nb")
            yield reg(f"  – {cfg['nome']} {nb}: pulado — NB não indicado pelo usuário", "muted")
            continue
        # CCON/CRER: baixar só as espécies relacionadas à pretensão. As demais existem no
        # SAT mas são puladas — REGISTRADAS no log "por não ser relacionado à pretensão".
        # Um NB explicitamente indicado pelo usuário SOBREPÕE o filtro por espécie.
        if (cfg.get("filtra_especie") and not nb_indicado
                and not especie_relacionada(pretensao, _achar_especie(row))):
            esp = _achar_especie(row)
            pulados.append(f"{cfg['nome']} {nb} — pulado por não ser relacionado à pretensão")
            _inv_add(cfg["key"], nb, "", "nao_relacionado")
            yield reg(f"  – {cfg['nome']} {nb} ({esp}): pulado por não ser relacionado à "
                      f"pretensão", "muted")
            continue
        # Consignados: não faz sentido consultar benefício INDEFERIDO — pular (decisão
        # do usuário 2026-07-21) para poupar consultas e não disparar o antirobô à toa.
        if cfg["nome"] == "HISCONSIGNADOS" and "INDEFER" in sit:
            _inv_add(cfg["key"], nb, "", "nao_consultado")
            yield reg(f"  – HISCONSIGNADOS {nb}: benefício {sit} — não consultado", "muted")
            continue
        if cfg["nome"] == "CCON":
            base = f"CCON {nb} {sit}"
        elif cfg["nome"] == "CRER":
            esp = sat_utils.abreviar(_achar_especie(row))
            base = f"CRER {esp} {nb} {sit}"
        elif cfg["nome"] == "HISCONSIGNADOS":
            base = f"HISCONSIGNADOS {nb}"
        elif cfg["nome"] == "LAUDO MEDICO":
            base = f"LAUDO MEDICO {nb}"
        else:
            base = f"{cfg['nome']} {nb}"
        nome_arq = _final_nome(base)
        # Verificação prévia: já no acervo → pula (retomada sem re-baixar/sobrescrever).
        if _ja_no_acervo(nome_arq):
            _inv_add(cfg["key"], nb, nome_arq, "baixado")
            yield reg(f"  ⏭ {nome_arq}: já baixado — pulando", "muted")
            continue
        out = {}
        async for ev in _clicar_abre_aba(
                page, ctx, (lambda idx=i: page.evaluate(_JS_ABRIR_PDF_LINHA, idx)),
                out, reg, nome_doc=base, alvo_norm=alvo, garantido=cfg.get("garantido", False)):
            yield ev
        nova, desf, msg = out["nova"], out["desfecho"], out.get("msg", "")
        _k = cfg["key"]
        if desf == "aba" and nova:
            ok = await _baixar_aba(ctx, nova, os.path.join(pasta_destino, nome_arq))
            if ok:
                salvos.append(nome_arq)
                _inv_add(_k, nb, nome_arq, "baixado")
                yield reg(f"  ✓ {nome_arq}", "ok")
            else:
                pulados.append(f"{nome_arq} (falha no download)")
                _inv_add(_k, nb, nome_arq, "falha")
                yield reg(f"  ✗ {nome_arq}: falha no download", "erro")
        elif desf == "sem_dados":
            pulados.append(f"{base} (sem dados)")
            _inv_add(_k, nb, "", "sem_dados")
            yield reg(f"  – {base}: não retornou dados", "muted")
        elif desf == "indisponivel":
            pulados.append(f"{base} (indisponível)")
            _inv_add(_k, nb, "", "indisponivel")
            yield reg(f"  – {base}: {msg or 'documento indisponível'}", "muted")
        else:
            pulados.append(f"{base} (antirobô — não recuperado)")
            _inv_add(_k, nb, "", "antirobo")
            yield reg(f"  ✗ {base}: proteção antirobô — não recuperado após {_MAX_TENT - 1} "
                      f"esperas de {_PAUSA_BLOQUEIO_MS // 1000}s", "erro")


async def _baixar_hiscre(page, ctx, pasta_destino, salvos, pulados, reg):
    """HISCRE: preenche De = (mês atual − 5 anos) e até = mês atual (MM/AAAA) e confirma."""
    from datetime import date
    hoje = date.today()
    de = f"{hoje.month:02d}/{hoje.year - 5}"
    ate = f"{hoje.month:02d}/{hoje.year}"
    with contextlib.suppress(Exception):
        await page.evaluate(_JS_CAMPO_MODAL, [0, de])
        await page.wait_for_timeout(250)
        await page.evaluate(_JS_CAMPO_MODAL, [1, ate])
        await page.wait_for_timeout(250)
    nome_arq = _final_nome("HISCRE 5 anos")
    out = {}
    async for ev in _clicar_abre_aba(
            page, ctx,
            (lambda: page.evaluate(_JS_BTN_MODAL, r"^(ok|consultar|abrir pdf|confirmar)$")),
            out, reg, nome_doc="HISCRE", alvo_norm="credito", garantido=True):
        yield ev
    nova, desf, msg = out["nova"], out["desfecho"], out.get("msg", "")
    if desf == "aba" and nova:
        ok = await _baixar_aba(ctx, nova, os.path.join(pasta_destino, nome_arq))
        if ok:
            salvos.append(nome_arq)
            _inv_add("hiscre", "", nome_arq, "baixado")
            yield reg(f"  ✓ {nome_arq} (De {de} até {ate})", "ok")
        else:
            pulados.append("HISCRE (falha no download)")
            _inv_add("hiscre", "", "", "falha")
            yield reg("  ✗ HISCRE: falha no download", "erro")
    elif desf == "sem_dados":
        pulados.append("HISCRE (sem dados)")
        _inv_add("hiscre", "", "", "sem_dados")
        yield reg("  – HISCRE: não retornou dados", "muted")
    elif desf == "indisponivel":
        pulados.append("HISCRE (indisponível)")
        _inv_add("hiscre", "", "", "indisponivel")
        yield reg(f"  – HISCRE: {msg or 'documento indisponível'}", "muted")
    else:
        pulados.append("HISCRE (antirobô — não recuperado)")
        _inv_add("hiscre", "", "", "antirobo")
        yield reg("  ✗ HISCRE: proteção antirobô — não recuperado (doc sempre existe)", "erro")


async def _grupo2_stream(page, ctx, nome, pasta_destino, salvos, pulados, reg, sel=None,
                         pretensao=""):
    """Baixa os documentos do Grupo 2 (cada um abre em modal). `sel` = keys selecionadas
    (None = todas). `pretensao` = filtra CCON/CRER por espécie relacionada."""
    yield reg("Baixando documentos do Grupo 2 (modais)…", "novo")
    for cfg in _GRUPO2:
        if sel is not None and cfg["key"] not in sel:
            _inv_add(cfg["key"], "", "", "nao_selecionado")
            yield reg(f"  · {cfg['label']}: não baixado (desativado para esta pretensão)", "muted")
            continue
        # HISCRE é um único arquivo determinístico: se já está no acervo, pula ANTES de
        # abrir o modal (retomada). Os demais (modais-tabela) checam por linha adiante.
        if cfg["tipo"] == "hiscre" and _ja_no_acervo(_final_nome("HISCRE 5 anos")):
            _inv_add(cfg["key"], "", _final_nome("HISCRE 5 anos"), "baixado")
            yield reg("  ⏭ HISCRE: já baixado — pulando", "muted")
            continue
        alvo = _norm(cfg.get("titulo") or cfg["label"])
        garantido = cfg.get("garantido", False)
        out = {}
        async for ev in _abrir_modal(page, cfg["label"], out, reg, nome_doc=cfg["label"],
                                     alvo_norm=alvo, garantido=garantido):
            yield ev
        estado = out["estado"]
        msg = out.get("msg", "")
        if estado == "sem_dados":
            pulados.append(f"{cfg['nome']} (sem dados)")
            _inv_add(cfg["key"], "", "", "sem_dados")
            yield reg(f"  – {cfg['label']}: não retornou dados", "muted")
            continue
        if estado == "indisponivel":
            pulados.append(f"{cfg['nome']} (indisponível)")
            _inv_add(cfg["key"], "", "", "indisponivel")
            yield reg(f"  – {cfg['label']}: {msg or 'documento indisponível'}", "muted")
            continue
        if estado == "bloqueio":
            pulados.append(f"{cfg['nome']} (antirobô — não recuperado)")
            _inv_add(cfg["key"], "", "", "antirobo")
            yield reg(f"  ✗ {cfg['label']}: proteção antirobô — não recuperado", "erro")
            continue
        if cfg["tipo"] == "hiscre":
            async for ev in _baixar_hiscre(page, ctx, pasta_destino, salvos, pulados, reg):
                yield ev
        else:
            async for ev in _baixar_modal_tabela(
                    page, ctx, cfg, nome, pasta_destino, salvos, pulados, reg, pretensao):
                yield ev
        await _fechar_modal(page)


async def _pat_stream(page, ctx, nome, pasta_destino, salvos, pulados, reg):
    """PAT / 2ª Via dos Processos: baixa todas as tarefas (todas as páginas) numa pasta
    temp e deduplica por (PROTOCOLO DE REQUERIMENTO interno + páginas), mantendo a
    PRINCIPAL e nomeando PROCADM (sat_utils.processar_pat)."""
    yield reg("Baixando processos administrativos (PAT / 2ª Via)…", "novo")
    out = {}
    async for ev in _abrir_modal(page, "Tarefas PAT", out, reg, nome_doc="Tarefas PAT",
                                 alvo_norm="tarefa", garantido=True):
        yield ev
    estado = out["estado"]
    if estado != "modal":  # rótulo alternativo
        out = {}
        async for ev in _abrir_modal(page, "Tarefas", out, reg, nome_doc="Tarefas PAT",
                                     alvo_norm="tarefa", garantido=True):
            yield ev
        estado = out["estado"]
    msg = out.get("msg", "")
    if estado == "sem_dados":
        pulados.append("PAT (sem dados)")
        _inv_add("pat", "", "", "sem_dados")
        yield reg("  – PAT: não retornou dados", "muted")
        return
    if estado == "indisponivel":
        pulados.append("PAT (indisponível)")
        _inv_add("pat", "", "", "indisponivel")
        yield reg(f"  – PAT: {msg or 'documento indisponível'}", "muted")
        return
    if estado != "modal":
        pulados.append("PAT (antirobô — não recuperado)")
        _inv_add("pat", "", "", "antirobo")
        yield reg("  ✗ PAT: modal não abriu — proteção antirobô (doc sempre existe)", "erro")
        return

    tmp = Path(pasta_destino) / "_pat_tmp"
    with contextlib.suppress(Exception):
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    mapa: dict[str, dict] = {}
    seq = 0
    pagina = 1
    vistos: set[str] = set()  # protocolos já processados (evita duplicatas na paginação)
    while True:
        linhas = await page.evaluate(_JS_LINHAS_MODAL)
        for i, row in enumerate(linhas):
            cells = _cells_dados(row)
            prot_i = next((k for k, c in enumerate(cells) if re.fullmatch(r"\d{6,}", c)), None)
            if prot_i is None:
                continue  # cabeçalho / linha "nenhum registro"
            prot = cells[prot_i]
            # dedup: se este protocolo já foi processado (relido na paginação), pula.
            if prot in vistos:
                continue
            vistos.add(prot)
            # download por SUBITEM (complementar): só as tarefas escolhidas (ou "pat|" = todas).
            if _ALVOS is not None and f"pat|{prot}" not in _ALVOS and "pat|" not in _ALVOS:
                continue
            servico = cells[prot_i + 1] if prot_i + 1 < len(cells) else ""
            # Filtro inicial por PROTOCOLO (indicado pelo usuário): pula tarefas de
            # protocolo não indicado — REGISTRA no log. Só se aplica ao PAT.
            protocolo_indicado = _PROTOCOLO_FILTRO is not None and prot in _PROTOCOLO_FILTRO
            if _PROTOCOLO_FILTRO is not None and not protocolo_indicado:
                _inv_add("pat", prot, "", "nao_filtrado_protocolo", rotulo=servico)
                pulados.append(f"PAT {servico} (protocolo {prot}) — pulado (protocolo não "
                               f"indicado pelo usuário)")
                yield reg(f"  – tarefa {prot} ({servico}): pulada — protocolo não indicado "
                          f"pelo usuário", "muted")
                continue
            # Verificação prévia: PROCADM deste protocolo já no acervo → pula (retomada).
            if _pat_ja_no_acervo(prot):
                _inv_add("pat", prot, "", "baixado", rotulo=servico)
                yield reg(f"  ⏭ tarefa {prot} ({servico}): já baixada — pulando", "muted")
                continue
            # Espécies de PAT irrelevantes (lista de ignorados): pula ANTES de baixar. Um
            # protocolo indicado EXPLICITAMENTE pelo usuário SOBREPÕE a lista de ignorados
            # (mesmo padrão do NB sobre o filtro por espécie no Grupo 2).
            if not protocolo_indicado and _pat_ignorado(servico):
                _inv_add("pat", prot, "", "ignorado", rotulo=servico)
                pulados.append(f"PAT {servico} (protocolo {prot}) — ignorado (espécie na lista de ignorados)")
                yield reg(f"  – tarefa {prot} ({servico}): ignorada (espécie de processo "
                          f"administrativo na lista de ignorados)", "muted")
                continue
            out = {}
            async for ev in _clicar_abre_aba(
                    page, ctx, (lambda idx=i: page.evaluate(_JS_ABRIR_PDF_LINHA, idx)),
                    out, reg, nome_doc=f"tarefa {prot}", garantido=True):
                yield ev
            nova, desf, msg = out["nova"], out["desfecho"], out.get("msg", "")
            if desf == "aba" and nova:
                seq += 1
                tmpnome = f"pat_{seq:03d}.pdf"
                ok = await _baixar_aba(ctx, nova, str(tmp / tmpnome))
                if ok:
                    mapa[tmpnome] = {"servico": servico, "protocolo": prot}
                    yield reg(f"  ↓ tarefa {prot} ({servico}) baixada", "muted")
                else:
                    _inv_add("pat", prot, "", "falha", rotulo=servico)
                    yield reg(f"  ✗ tarefa {prot}: falha no download", "erro")
            elif desf == "sem_dados":
                _inv_add("pat", prot, "", "sem_dados", rotulo=servico)
                yield reg(f"  – tarefa {prot}: sem dados", "muted")
            elif desf == "indisponivel":
                _inv_add("pat", prot, "", "indisponivel", rotulo=servico)
                yield reg(f"  – tarefa {prot}: {msg or 'documento indisponível'}", "muted")
            else:
                _inv_add("pat", prot, "", "antirobo", rotulo=servico)
                yield reg(f"  ✗ tarefa {prot}: proteção antirobô — não recuperado", "erro")
        avancou = await page.evaluate(_JS_PAG_PROX)
        if not avancou:
            break
        pagina += 1
        await _ritmo(page)  # avançar de página é uma consulta — ritmo governa a transição
        if pagina > 50:  # trava de segurança
            yield reg("  ⚠ PAT: >50 páginas — interrompido por segurança", "erro")
            break
    await _fechar_modal(page)

    if not mapa:
        pulados.append("PAT (sem tarefas)")
        yield reg("  – PAT: nenhuma tarefa encontrada", "muted")
    else:
        try:
            # Em pedidos EXPLÍCITOS/complementares (o usuário escolheu os itens, _ALVOS
            # definido), a regra dos 5 anos NÃO se aplica — baixa independentemente da idade.
            mant, desc, pul_prazo, pul_nb = sat_utils.processar_pat(
                str(tmp), pasta_destino, mapa, _ROTULO_ARQ, _NB_FILTRO,
                aplicar_regra_5anos=(_ALVOS is None))
            yield reg(f"  PAT: {len(mapa)} tarefa(s) baixada(s) → {len(mant)} principal(is) "
                      f"mantido(s), {len(desc)} descartada(s) (subtarefa/duplicata)"
                      + (f", {len(pul_prazo)} pulado(s) (>5 anos)" if pul_prazo else "")
                      + (f", {len(pul_nb)} pulado(s) (NB não indicado)" if pul_nb else ""),
                      "novo")
            for _f, nm in mant:
                salvos.append(nm)
                _inv_add("pat", mapa.get(_f, {}).get("protocolo", ""), nm, "baixado",
                         rotulo=mapa.get(_f, {}).get("servico", ""))
                yield reg(f"  ✓ {nm}", "ok")
            for _a, motivo in desc:
                _inv_add("pat", mapa.get(_a, {}).get("protocolo", ""), "", "descartado",
                         rotulo=mapa.get(_a, {}).get("servico", ""))
                yield reg(f"  🗑 {motivo}", "muted")
            for _p, motivo in pul_prazo:
                _inv_add("pat", mapa.get(_p, {}).get("protocolo", ""), "", "pulado_prazo",
                         rotulo=mapa.get(_p, {}).get("servico", ""))
                pulados.append(f"PAT {motivo}")
                yield reg(f"  ↷ {motivo}", "muted")
            for _p, motivo in pul_nb:
                _inv_add("pat", mapa.get(_p, {}).get("protocolo", ""), "", "nao_filtrado_nb",
                         rotulo=mapa.get(_p, {}).get("servico", ""))
                pulados.append(f"PAT {motivo}")
                yield reg(f"  ↷ {motivo}", "muted")
        except Exception as e:  # noqa: BLE001
            pulados.append(f"PAT (dedup falhou: {e})")
            yield reg(f"  ✗ PAT: deduplicação/nomeação falhou: {e}", "erro")
    with contextlib.suppress(Exception):
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)


# Tipos que podem ser compilados num único PDF (nome-base → prefixo do arquivo salvo).
_TIPOS_COMPILAVEIS = [("CCON", "CCON "), ("CRER", "CRER "),
                      ("LAUDO MEDICO", "LAUDO MEDICO "), ("LAUDO SOCIAL", "LAUDO SOCIAL "),
                      ("HISCONSIGNADOS", "HISCONSIGNADOS ")]
# Subpasta (dentro da pasta do PAJ) onde ficam os originais após a compilação — NÃO é
# anexada (o anexador lê só a pasta principal). Apagada após a anexação (sat_service).
SUBPASTA_ORIGINAIS_COMPILADOS = "Compilados (originais)"


async def _compilar_por_tipo(pasta_destino, salvos, reg):
    """Regra geral: se um tipo (CCON/CRER/Laudo Médico/Laudo Social) tem MAIS de
    _COMPILAR_LIMIAR arquivos, une-os num único PDF em ordem de NB DECRESCENTE, para reduzir
    os anexos da movimentação. Os originais vão p/ a subpasta 'Compilados (originais)' (não
    anexada); são apagados só após a anexação do compilado (limpeza no sat_service)."""
    if not _COMPILAR:
        return
    import shutil as _sh
    pasta = Path(pasta_destino)
    orig_dir = pasta / SUBPASTA_ORIGINAIS_COMPILADOS
    for nome_tipo, prefixo in _TIPOS_COMPILAVEIS:
        arquivos = sorted(pasta.glob(f"{prefixo}*.pdf"),
                          key=lambda p: sat_utils.nb_digits(p.name), reverse=True)
        if len(arquivos) <= _COMPILAR_LIMIAR:
            continue
        destino = pasta / f"{nome_tipo} - COMPILADO {len(arquivos)} documentos.pdf"
        try:
            n_pag = sat_utils.merge_pdfs([str(p) for p in arquivos], str(destino))
        except Exception as e:  # noqa: BLE001
            yield reg(f"  ✗ Falha ao compilar {nome_tipo}: {e}", "erro")
            continue
        orig_dir.mkdir(parents=True, exist_ok=True)
        for p in arquivos:
            with contextlib.suppress(Exception):
                _sh.move(str(p), str(orig_dir / p.name))
            if p.name in salvos:
                salvos.remove(p.name)
        salvos.append(destino.name)
        yield reg(f"  📎 {nome_tipo}: {len(arquivos)} documentos compilados em 1 PDF "
                  f"({n_pag} pág., ordem de NB decrescente) → {destino.name}", "novo")


async def baixar_cidadao_stream(cpf: str, nome: str, pasta_destino: str,
                                delay_s: float = 5.0, bloqueio_s: float = 45.0,
                                tipos=None, pretensao="", rotulo="", alvos=None,
                                nbs=None, protocolos=None, compilar=False, compilar_limiar=3,
                                pat_ignorar=None, pasta_acervo=None):
    """Consulta o CPF no SAT (usuário resolve o CAPTCHA na janela) e baixa o Grupo 1
    (1 clique), o Grupo 2 (modais: CCON, CRER, HISCRE, HISCONSIGNADOS, LAUDO MEDICO) e
    o PAT (2ª Via dos Processos, com deduplicação). `delay_s` = ritmo preventivo entre
    consultas (padrão 5s; 0 desliga). `bloqueio_s` = espera ao detectar antirobô
    (padrão 45s). `tipos` = coleção de keys de TIPOS_DOCUMENTOS a baixar (None = todos).
    `nbs` = coleção de NBs (qualquer formato) p/ filtrar Grupo 2 + PAT (None/vazio = sem
    filtro). `protocolos` = coleção de protocolos (qualquer formato) p/ filtrar SÓ o PAT
    (None/vazio = sem filtro — baixa todas as tarefas). `compilar`/`compilar_limiar` =
    compilar por tipo quando > limiar (só no download principal). `pat_ignorar` = espécies
    de PAT (Serviço) a NÃO baixar.
    `pasta_acervo` = pasta do acervo final para VERIFICAÇÃO PRÉVIA (pula docs já presentes;
    permite RETOMAR downloads interrompidos); None = usa `pasta_destino`. O log é gravado
    INCREMENTALMENTE (a cada documento) — uma interrupção deixa o inventário parcial em disco.
    Gera eventos SSE; termina com {'done':True,'resultado':{...}}."""
    global _RITMO_MS, _PAUSA_BLOQUEIO_MS, _ROTULO_ARQ, _INVENTARIO, _ALVOS
    global _NB_FILTRO, _PROTOCOLO_FILTRO, _SOMENTE_PAT, _COMPILAR, _COMPILAR_LIMIAR, _PAT_IGNORAR, _PASTA_ACERVO, _LOG_PERSIST
    _RITMO_MS = max(0, int(float(delay_s) * 1000))
    _PAUSA_BLOQUEIO_MS = max(0, int(float(bloqueio_s) * 1000))
    # filtro de NB: aceita formatado ("726.015.826-4") ou dígitos crus ("7260158264");
    # normaliza para só dígitos. Vazio/None = sem filtro.
    _NB_FILTRO = {re.sub(r"\D", "", str(x)) for x in nbs if re.sub(r"\D", "", str(x))} if nbs else None
    # filtro de PROTOCOLO (só PAT): mesma normalização (só dígitos). Vazio/None = sem filtro.
    _PROTOCOLO_FILTRO = ({re.sub(r"\D", "", str(x)) for x in protocolos if re.sub(r"\D", "", str(x))}
                          if protocolos else None)
    _COMPILAR = bool(compilar)
    _COMPILAR_LIMIAR = max(1, int(compilar_limiar or 3))
    _PAT_IGNORAR = list(pat_ignorar) if pat_ignorar else []  # espécies de PAT a não baixar
    _ROTULO_ARQ = rotulo or ""  # prefixo dos nomes (ex.: "INSTITUIDOR - ")
    _INVENTARIO = []  # zera o inventário estruturado desta rodada
    _ALVOS = set(alvos) if alvos else None  # subitens específicos (download complementar)
    _PASTA_ACERVO = str(pasta_acervo or pasta_destino)  # verificação prévia (já baixado)
    sel = None if tipos is None else set(tipos)  # None = todos os tipos
    # "somente PAT": derivado do próprio `sel` resolvido (funciona tanto quando a rota
    # força tipos=["pat"] pela opção explícita, quanto se um dia uma config de pretensão
    # resolver para só "pat") — controla só a MENSAGEM de log (o skip do Grupo 1/2 já
    # acontece via `sel`); o filtro por protocolo continua funcionando dentro do PAT.
    _SOMENTE_PAT = sel == {"pat"}
    Path(pasta_destino).mkdir(parents=True, exist_ok=True)
    salvos: list[str] = []
    pulados: list[str] = []
    log_msgs: list[dict] = []
    from datetime import datetime as _dt
    _ts_ini = _dt.now().isoformat(timespec="seconds")

    def _gravar_log() -> None:
        """Grava o _log_download.json com o estado ATUAL (chamado a cada documento e no
        fim). Padrão do painel: log incremental — interrupção deixa o parcial em disco."""
        import json as _json
        Path(pasta_destino, "_log_download.json").write_text(
            _json.dumps({"ts": _ts_ini, "cpf": cpf, "nome": nome, "salvos": salvos,
                         "pulados": pulados, "inventario": list(_INVENTARIO),
                         "eventos": log_msgs, "parcial": True},
                        ensure_ascii=False, indent=2), encoding="utf-8")

    _LOG_PERSIST = _gravar_log  # _inv_add passa a gravar o log a cada documento

    def reg(msg: str, level: str = "info") -> dict:
        e = _ev(msg, level)
        log_msgs.append(e)
        return e

    browser, page = await _connect()
    ctx = page.context
    try:
        with contextlib.suppress(Exception):
            await page.bring_to_front()
            await page.goto(SAT_CONSULTA, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1000)
        if not _status_dict(page.url)["logado"]:
            yield {"done": True, "resultado": {"ok": False,
                   "detalhe": "Sessão do SAT expirada — clique em 'Abrir SAT / Login' e refaça o login."}}
            return

        # --- consulta por CPF ---
        dig = "".join(c for c in cpf if c.isdigit())
        yield reg(f"Consultando o CPF {cpf}…", "novo")
        try:
            await page.click('[id="formFiltrosPF:txtCpf"]', timeout=10000)
            await page.fill('[id="formFiltrosPF:txtCpf"]', "")
            await page.type('[id="formFiltrosPF:txtCpf"]', dig, delay=25)
            await page.click('[id="formFiltrosPF:btnConsultarPF"]', timeout=10000)
        except Exception as e:  # noqa: BLE001
            yield {"done": True, "resultado": {"ok": False, "detalhe": f"Falha ao iniciar a consulta: {e}"}}
            return
        yield reg("⚠ Resolva o CAPTCHA na JANELA do SAT e clique em 'Enviar'. Aguardando… (até 3 min)", "info")
        try:
            await page.wait_for_function(_JS_TEM_CONSULTAR, timeout=180000)
        except Exception:
            yield {"done": True, "resultado": {"ok": False,
                   "detalhe": "Não detectei os dados do cidadão (CAPTCHA não resolvido a tempo?)."}}
            return
        await page.wait_for_timeout(800)
        if _SOMENTE_PAT:
            yield reg("Cidadão localizado. Opção \"Baixar exclusivamente processo "
                      "administrativo\" ativa — pulando Grupo 1 e Grupo 2, indo direto ao PAT.",
                      "ok")
        else:
            yield reg("Cidadão localizado. Baixando os documentos (Grupo 1)…", "ok")

        # --- Grupo 1 ---
        for label, prefixo, garantido, key in _GRUPO1:
            if sel is not None and key not in sel:
                _inv_add(key, "", "", "nao_selecionado")
                if not _SOMENTE_PAT:
                    yield reg(f"  · {prefixo}: não baixado (desativado para esta pretensão)", "muted")
                continue
            # Verificação prévia: já no acervo → pula (retomada sem re-baixar).
            nome_arq_g1 = _final_nome(f"{prefixo} {nome}")
            if _ja_no_acervo(nome_arq_g1):
                _inv_add(key, "", nome_arq_g1, "baixado")
                yield reg(f"  ⏭ {prefixo}: já baixado — pulando", "muted")
                continue
            alvo_norm = _norm(label)
            nova, desfecho, msg = None, "bloqueio", ""
            # Quatro desfechos por clique: "aba" (abriu o PDF) · "sem_dados" (aviso expresso
            # de ausência) · "indisponivel" (aviso expresso de inacessibilidade) · "bloqueio"
            # (SILÊNCIO total → anti-robô → re-tentar). Qualquer aviso EXPRESSO NOVO (frescor
            # via marcação) encerra sem disparar o skip temporal do antirobô.
            for tentativa in range(1, _MAX_TENT + 1):
                antes = set(ctx.pages)
                await page.evaluate(_JS_MARCAR_BANNERS)  # marca banners RESIDUAIS como vistos
                clicou = await page.evaluate(_JS_CLK, label)
                if clicou:
                    for _ in range(16):  # aguarda ~8s por aba ou aviso do documento
                        await page.wait_for_timeout(500)
                        novas = [p for p in ctx.pages if p not in antes]
                        if novas:
                            nova, desfecho = novas[0], "aba"
                            break
                        banner = _norm(await page.evaluate(_JS_BANNER_NOVO))
                        cls = _classificar_banner(banner)
                        if cls:
                            desfecho, msg = cls, banner
                            break
                if desfecho in ("aba", "sem_dados", "indisponivel"):
                    break
                # SILÊNCIO: nem aba nem aviso expresso → proteção antirobô ativada → pausa
                # (fixo) e re-tenta. Só o silêncio total dispara o skip temporal.
                if tentativa < _MAX_TENT:
                    cert = "" if garantido else " (provável)"
                    yield reg(f"  🛡 proteção antirobô{cert} ativada em {prefixo} — aguardando "
                              f"{_PAUSA_BLOQUEIO_MS // 1000}s e re-tentando "
                      f"({tentativa}/{_MAX_TENT - 1})", "muted")
                    await page.wait_for_timeout(_PAUSA_BLOQUEIO_MS)
            if desfecho == "aba" and nova:
                with contextlib.suppress(Exception):
                    await nova.wait_for_load_state("domcontentloaded", timeout=15000)
                nome_arq = _final_nome(f"{prefixo} {nome}")
                ok = await _baixar_pdf(ctx, _pdf_url(nova.url), os.path.join(pasta_destino, nome_arq))
                with contextlib.suppress(Exception):
                    await nova.close()
                if ok:
                    salvos.append(nome_arq)
                    _inv_add(key, "", nome_arq, "baixado")
                    yield reg(f"  ✓ {nome_arq}", "ok")
                else:
                    pulados.append(f"{prefixo} (falha no download)")
                    _inv_add(key, "", "", "falha")
                    yield reg(f"  ✗ {prefixo}: falha no download", "erro")
            elif desfecho == "sem_dados":
                pulados.append(f"{prefixo} (sem dados)")
                _inv_add(key, "", "", "sem_dados")
                yield reg(f"  – {prefixo}: não retornou dados", "muted")
            elif desfecho == "indisponivel":
                pulados.append(f"{prefixo} (indisponível)")
                _inv_add(key, "", "", "indisponivel")
                yield reg(f"  – {prefixo}: {msg or 'documento indisponível'}", "muted")
            else:  # bloqueio persistente após as tentativas
                pulados.append(f"{prefixo} (antirobô — não recuperado)")
                _inv_add(key, "", "", "antirobo")
                yield reg(f"  ✗ {prefixo}: proteção antirobô — não recuperado após "
                          f"{_MAX_TENT - 1} esperas de {_PAUSA_BLOQUEIO_MS // 1000}s — "
                          f"reveja manualmente", "erro")
            await _ritmo(page)  # ritmo: delay entre consultas

        # --- Grupo 2 (modais) ---
        if sel is None or any(c["key"] in sel for c in _GRUPO2):
            with contextlib.suppress(Exception):
                await _fechar_modal(page)
            async for ev in _grupo2_stream(page, ctx, nome, pasta_destino, salvos, pulados,
                                           reg, sel, pretensao):
                yield ev

        # --- PAT (2ª Via dos Processos) ---
        if sel is None or "pat" in sel:
            with contextlib.suppress(Exception):
                await _fechar_modal(page)
            async for ev in _pat_stream(page, ctx, nome, pasta_destino, salvos, pulados, reg):
                yield ev
        else:
            _inv_add("pat", "", "", "nao_selecionado")
            yield reg("  · Tarefas PAT: não baixado (desativado para esta pretensão)", "muted")

        # --- Compilação por tipo (CCON/CRER/Laudos) quando > limiar ---
        if _COMPILAR:
            async for ev in _compilar_por_tipo(pasta_destino, salvos, reg):
                yield ev

        det = f"{len(salvos)} documento(s) baixado(s)."
        yield {"done": True, "resultado": {"ok": True, "detalhe": det,
               "salvos": salvos, "pulados": pulados, "pasta": pasta_destino}}
    finally:
        # Log já foi gravado INCREMENTALMENTE a cada documento; aqui grava a versão FINAL
        # (marcando parcial=False). Se a rotina foi interrompida (perda de conexão), o
        # parcial gravado no último _inv_add permanece em disco para permitir a retomada.
        with contextlib.suppress(Exception):
            import json as _json
            Path(pasta_destino, "_log_download.json").write_text(
                _json.dumps({"ts": _ts_ini, "cpf": cpf, "nome": nome, "salvos": salvos,
                             "pulados": pulados, "inventario": list(_INVENTARIO),
                             "eventos": log_msgs, "parcial": False},
                            ensure_ascii=False, indent=2), encoding="utf-8")
        _LOG_PERSIST = None  # desliga a persistência incremental desta rodada
        _PASTA_ACERVO = None
        with contextlib.suppress(Exception):
            await browser.close()
