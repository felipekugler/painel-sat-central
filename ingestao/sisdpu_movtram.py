"""Movimentar / Tramitar / Concluir PAJ no SISDPU (Playwright headless, async).

Portado do PAINEL COMUNICAÇÕES (`ingestao/sisdpu_client.py`), onde o mecanismo foi
mapeado e validado em teste real. Aqui reusa a infra de sessão/login do
`ingestao.sisdpu_client` deste painel (browser/page globais, login com as
credenciais do cofre) — não abre um segundo browser.

Funções públicas:
- `listar_caixa_estruturada()` — lê a caixa de entrada do usuário por linha, com
  `id_tramite` (data-rk), `id` (id_processo), `paj`, `assistido`, `descricao`.
- `movimentar_paj(pajs, descricao, fase, anexos, tipo_arquivo, dry_run)`.
- `tramitar_paj(pajs, grupo, usuarios, descricao, prazo_data, prazo_desc, dry_run)`.
- `concluir_paj(id_tramites, dry_run)` — admite na caixa de grupo + conclui.
- `listar_grupos_unidade(id_processo)` — lê o dropdown de grupos da unidade.
"""

from __future__ import annotations

import contextlib
import mimetypes
import re
import unicodedata
from pathlib import Path

from ingestao import sisdpu_client as _sc

# --- infra reusada do cliente deste painel (mesmo browser/page globais) ---
_sel = _sc._sel
SISDPU_URL = _sc.SISDPU_URL
_get_page = _sc._get_page
_ensure_logged_in = _sc._ensure_logged_in
_wait_pf_ajax = _sc._wait_pf_ajax
_log = _sc._log

# --- constantes de formulário / negócio ---
_MOV_FORM = "formHonorario"
_TRAM_FORM = "form_tramite"
_TIPOS_ANEXO_OK = {"doc", "docx", "odt", "png", "jpg", "jpeg", "gif", "pdf"}
_MSG_CONFLITO_SESSAO = "Conflito de sessão do SISDPU — feche a outra sessão e tente de novo."


# ---------------------------------------------------------------------------
# JS auxiliares
# ---------------------------------------------------------------------------

# Seleciona <option> por TEXTO num <select> nativo (.epaj_select_formata) + dispara
# 'change' (o AJAX do PrimeFaces reage). args = [selectId, textoAlvo, exato].
_JS_SELECT_OPT = r"""
(args) => {
  const [selId, alvo, exato] = args;
  const s = document.getElementById(selId);
  if (!s) return {ok:false, err:'select-inexistente'};
  const n = t => (t||'').normalize('NFD').replace(/[̀-ͯ]/g,'').toLowerCase().trim();
  let val=null, txt=null;
  for (const o of s.options){ if (n(o.text)===n(alvo)){ val=o.value; txt=o.text.trim(); break; } }
  if (val===null && !exato) for (const o of s.options){ if (n(o.text).includes(n(alvo))){ val=o.value; txt=o.text.trim(); break; } }
  if (val===null) return {ok:false, opts:[...s.options].map(o=>o.text.trim())};
  s.value = val; s.dispatchEvent(new Event('change', {bubbles:true}));
  return {ok:true, val, txt};
}
"""

# Escreve na descrição (CKEditor) + input/textarea subjacentes. args=[txt, hidId, taId, raw].
_JS_SET_CKEDITOR = r"""
(args) => {
  const [txt, hidId, taId, raw] = args;
  const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const html = raw ? (txt||'') : esc(txt).replace(/\r\n|\r|\n/g, '<br>');
  let done=false, keys=[];
  if (window.CKEDITOR && CKEDITOR.instances) {
    keys = Object.keys(CKEDITOR.instances);
    for (const nm of keys) {
      if (nm.includes('editorValue')) {
        CKEDITOR.instances[nm].setData(html);
        try { CKEDITOR.instances[nm].updateElement(); } catch(e) {}
        done = true;
      }
    }
  }
  const hid = document.getElementById(hidId); if (hid) hid.value = html;
  const ta  = document.getElementById(taId);  if (ta)  ta.value  = html;
  return {done, keys};
}
"""

# Marca o checkbox "Todas as fases" (revela fases fora da lista curta). Por TEXTO do label.
_JS_MARCAR_TODAS_FASES = r"""
() => {
  const l = [...document.querySelectorAll('label')].find(x => /todas as fases/i.test(x.innerText||''));
  if (!l) return false;
  const td = l.closest('td') || l.parentElement;
  const box = td && td.querySelector('.ui-chkbox-box');
  if (!box) return false;
  const icon = box.querySelector('.ui-chkbox-icon');
  const jaMarcado = icon && /ui-icon-check/.test(icon.className);
  if (!jaMarcado) box.click();
  return true;
}
"""

# Maior nº de sequência de movimentação no detalhamento (para reportar "Mov X").
_JS_MAX_SEQ_MOV = r"""
() => {
  let max = null;
  for (const tr of document.querySelectorAll("table tbody tr")) {
    const cells = tr.querySelectorAll(":scope > td");
    if (cells.length < 6) continue;
    const seq = (cells[0].textContent || "").trim();
    const data = (cells[2].textContent || "").trim();
    if (!/^\d{1,4}$/.test(seq)) continue;
    if (!/\d{2}\/\d{2}\/\d{4}/.test(data)) continue;
    const n = parseInt(seq, 10);
    if (max === null || n > max) max = n;
  }
  return max;
}
"""

# Conflito de sessão (1 sessão por usuário): diálogo 'confirmDialogLogin' visível.
_JS_CONFLITO_LOGIN = r"""
() => !!document.querySelector('[id$="confirmDialogLogin_modal"]')
      || [...document.querySelectorAll('[id$="confirmDialogLogin"]')].some(d => d.offsetParent !== null)
"""

# --- Concluir: admitir (caixa de grupo) + concluir (caixa do usuário) ---
_JS_ATIVA_ABA_GRUPO = r"""() => {
    const links = document.querySelectorAll('.ui-tabs-nav a, a[href*="tabView:tab"]');
    for (const a of links) if ((a.textContent||'').toLowerCase().includes('caixa de entrada do grupo')) { a.click(); return true; }
    return false;
}"""
_JS_TRAMITE_NO_GRUPO = r"""(idt) => {
    for (const tr of document.querySelectorAll('tr[data-rk="'+idt+'"]')) {
        for (const a of tr.querySelectorAll('a, button'))
            if ((a.textContent||'').replace(/\s+/g,' ').trim().toLowerCase() === 'admitir') return true;
    }
    return false;
}"""
_JS_ADMITIR_TRAMITE = r"""(idt) => {
    for (const tr of document.querySelectorAll('tr[data-rk="'+idt+'"]')) {
        for (const a of tr.querySelectorAll('a, button'))
            if ((a.textContent||'').replace(/\s+/g,' ').trim().toLowerCase() === 'admitir') { a.click(); return true; }
    }
    return false;
}"""
_JS_TRAMITE_NA_CAIXA = r"""(idt) => {
    const t = document.querySelector('[id="tabView:caixaEntradaTab1Form:tramites"]');
    return !!(t && t.querySelector('tbody tr[data-rk="'+idt+'"]'));
}"""
_JS_MARCA_CHECKBOX = r"""(idt) => {
    const box = document.querySelector('[id="tabView:caixaEntradaTab1Form:tramites_'+idt+'_checkbox"]');
    if (!box) return false;
    if (!/ui-state-active/.test(box.getAttribute('class')||'')) box.click();
    return true;
}"""
_JS_CLICA_BTN_CONCLUIR = r"""() => {
    const b = document.querySelector('[id="tabView:caixaEntradaTab1Form:btnConcluir"]');
    if (!b) return false; b.click(); return true;
}"""

# Extrai a caixa de entrada do usuário por linha (data-rk = id_tramite). Colunas do
# SISDPU: [checkbox · Operações · Processo · Assistido · Data de Envio · Remetente ·
# Prazo · Descrição · —]. Robusto: captura `linha_texto` (todas as células) para o
# filtro por descrição não depender do índice exato.
_JS_CAIXA_USUARIO = r"""() => {
    const t = document.querySelector('[id="tabView:caixaEntradaTab1Form:tramites"]');
    if (!t) return {encontrado:false, itens:[]};
    const norm = s => (s||'').replace(/\s+/g,' ').trim();
    const itens = [];
    for (const tr of t.querySelectorAll('tbody tr[data-rk]')) {
        const idt = tr.getAttribute('data-rk') || '';
        const tds = [...tr.querySelectorAll(':scope > td')];
        let pIdx=-1, linkDet=null;
        for (let i=0;i<tds.length;i++){ const a=tds[i].querySelector('a[href*="detalhamentoProcesso"]'); if(a){pIdx=i;linkDet=a;break;} }
        if (pIdx<0) continue;
        const textoProc = norm(linkDet.textContent);
        const mPaj = textoProc.match(/(\d{4}\/\d{3}-\d+)\s*(.*)$/);
        const paj = mPaj?mPaj[1]:'';
        const assistidoInline = mPaj?norm(mPaj[2]).replace(/^\(\s*/,'').replace(/\s*\)$/,''):'';
        const href = linkDet.getAttribute('href')||'';
        const mId = href.match(/[?&]id=(\d+)/);
        const idProc = mId?mId[1]:'';
        const cell = i => tds[i]?norm(tds[i].textContent):'';
        itens.push({
            id_tramite: idt, id: idProc, paj,
            assistido: cell(pIdx+1) || assistidoInline,
            data_envio: cell(pIdx+2),
            remetente: cell(pIdx+3),
            prazo: cell(pIdx+4),
            descricao: cell(pIdx+5),
            linha_texto: tds.map(td=>norm(td.textContent)).filter(Boolean).join(' | '),
            url_detalhe: href ? (href.startsWith('http')?href:location.origin+href) : '',
        });
    }
    return {encontrado:true, itens};
}"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _norm_nome(s: str) -> str:
    """Normaliza p/ comparar: sem acentos, MAIÚSCULO, sem '(N PAJ'S)', 1 espaço."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\(\s*\d+\s*paj.*?\)", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip().upper()


async def _eval_seguro(page, js: str, tentativas: int = 5) -> dict:
    """page.evaluate tolerante a navegação (o contexto pode ser destruído por postback
    JSF no meio do evaluate) — re-tenta após a navegação assentar."""
    ultimo = None
    for _ in range(tentativas):
        try:
            return await page.evaluate(js)
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if "context was destroyed" in msg or "navigation" in msg or "execution context" in msg:
                ultimo = e
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("domcontentloaded", timeout=8000)
                await page.wait_for_timeout(500)
                continue
            raise
    if ultimo:
        raise ultimo
    return await page.evaluate(js)


async def _resolver_conflito_login(page) -> bool:
    """Se o diálogo de conflito de sessão estiver VISÍVEL, clica 'Desbloquear' (assume
    a sessão) e aguarda sumir. Retorna True se não há conflito (ou foi resolvido)."""
    conflito = await _eval_seguro(page, _JS_CONFLITO_LOGIN)
    if not conflito:
        with contextlib.suppress(Exception):
            from services import conexao_service
            conexao_service.marcar_conectado()
        return True
    _log.info("movtram: conflito de sessão detectado — clicando 'Desbloquear'.")
    btn = page.locator('[id$="confirmTramitacao"]').first
    with contextlib.suppress(Exception):
        if await btn.count():
            await btn.click(timeout=5000)
    with contextlib.suppress(Exception):
        await page.wait_for_selector('[id$="confirmDialogLogin_modal"]', state="hidden", timeout=8000)
    await page.wait_for_timeout(300)
    resolvido = not await _eval_seguro(page, _JS_CONFLITO_LOGIN)
    with contextlib.suppress(Exception):
        from services import conexao_service
        conexao_service.marcar_conectado() if resolvido else conexao_service.marcar_conflito(_MSG_CONFLITO_SESSAO)
    return resolvido


async def _ler_pajs_lista(page, input_id: str) -> list[str]:
    return await page.evaluate(
        """(id)=>{const s=document.getElementById(id);return s?[...s.options].map(o=>o.text.trim()).filter(Boolean):[];}""",
        input_id,
    )


async def _dialogos_visiveis(page) -> list[dict]:
    return await _eval_seguro(page, r"""
    () => {
      const vis = d => { const s=getComputedStyle(d); const r=d.getBoundingClientRect();
        return s.display!=='none' && s.visibility!=='hidden' && r.width>0 && r.height>0; };
      return [...document.querySelectorAll('.ui-confirm-dialog,.ui-dialog')].filter(vis).map(d=>({
        id: d.id,
        msg: ((d.querySelector('.ui-confirm-dialog-message,.ui-dialog-content')||{}).innerText||'').trim().slice(0,180),
        btns: [...d.querySelectorAll('button, a.ui-button')].filter(vis).map(b=>(b.innerText||'').trim()).filter(Boolean)
      }));
    }
    """)


async def _adicionar_paj_por_numero(page, form: str, add_btn_id: str, numero: str) -> None:
    ano, resto = numero.split("/")
    _unidade, num = resto.split("-")
    await page.evaluate(_JS_SELECT_OPT, [f"{form}:data", ano, True])
    await page.fill(_sel(f"{form}:numProcesso"), num)
    with contextlib.suppress(Exception):
        await page.locator(_sel(add_btn_id)).first.click(timeout=6000)
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(600)


async def _ler_seq_mov(page, id_processo: str) -> int | None:
    """Lê o nº de sequência da movimentação recém-criada (maior seq no detalhamento)."""
    try:
        url = f"{SISDPU_URL}/pages/atendimento/detalhamentoProcesso.xhtml?id={id_processo}"
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=10000)
        await page.wait_for_timeout(300)
        seq = await _eval_seguro(page, _JS_MAX_SEQ_MOV)
        return int(seq) if seq is not None else None
    except Exception:  # noqa: BLE001
        return None


async def _anexar_documentos_movimentacao(page, anexos: list[dict], tipo_arquivo: str) -> list[str]:
    """Anexa documentos à MOVIMENTAÇÃO (popup 'dlgModalArquivos' da fase). Para cada
    anexo: abre o popup, sobe o arquivo (buffer, preserva nome), Descrição = nome,
    Tipo de Arquivo = `tipo_arquivo`, 'Anexar'. Fecha o popup ao fim. Best-effort."""
    anexos = [a for a in (anexos or []) if a]
    if not anexos:
        return []
    anexados: list[str] = []
    with contextlib.suppress(Exception):
        await page.get_by_text("Adicionar Anexo", exact=True).first.click()
    with contextlib.suppress(Exception):
        await page.wait_for_selector(_sel(f"{_MOV_FORM}:arquivosFase_input"), timeout=10000)
    await page.wait_for_timeout(400)
    for a in anexos:
        caminho = a.get("caminho") or ""
        nome = a.get("nome") or (Path(caminho).name if caminho else "documento")
        if not caminho or not Path(caminho).exists():
            _log.warning("mov anexo ausente no disco, pulado: %s (%s)", nome, caminho)
            continue
        ext = (a.get("tipo") or Path(nome).suffix).lstrip(".").lower()
        if ext not in _TIPOS_ANEXO_OK:
            _log.warning("mov anexo de tipo não permitido, pulado: %s (.%s)", nome, ext)
            continue
        try:
            data = Path(caminho).read_bytes()
            mime = mimetypes.guess_type(nome)[0] or "application/octet-stream"
            await page.locator(_sel(f"{_MOV_FORM}:arquivosFase_input")).set_input_files(
                [{"name": nome, "mimeType": mime, "buffer": data}])
            desc_sel = _sel(f"{_MOV_FORM}:arquivosSalvar:0:desc")
            await page.wait_for_selector(desc_sel, timeout=10000)
            with contextlib.suppress(Exception):
                await page.fill(desc_sel, nome)
            tp = await page.evaluate(
                _JS_SELECT_OPT, [f"{_MOV_FORM}:arquivosSalvar:0:tipoArquivo", tipo_arquivo, False])
            if not tp.get("ok"):
                _log.warning("mov anexo: tipo %r não achado (opts=%s)", tipo_arquivo, tp.get("opts"))
            with contextlib.suppress(Exception):
                await page.locator(
                    'button[name^="%s:arquivosSalvar"]' % _MOV_FORM, has_text="Anexar").first.click()
            await _wait_pf_ajax(page)
            await page.wait_for_timeout(500)
            anexados.append(nome)
            _log.info("mov anexo incluído: %s (tipo=%s)", nome, tipo_arquivo)
        except Exception as e:  # noqa: BLE001
            _log.warning("mov falha ao anexar %s: %r", nome, e)
    with contextlib.suppress(Exception):
        fechar = page.locator('button[name^="%s:"]' % _MOV_FORM, has_text="Fechar").first
        if await fechar.is_visible():
            await fechar.click()
            await page.wait_for_timeout(400)
    with contextlib.suppress(Exception):
        aberto = await page.evaluate(
            """()=>{const d=document.getElementById('%s:dlgModalArquivos');
                if(!d)return false;const s=getComputedStyle(d);const r=d.getBoundingClientRect();
                return s.display!=='none'&&r.width>0;}""" % _MOV_FORM)
        if aberto:
            await page.evaluate(
                "()=>{try{PF('dlgModalArquivosWidget').hide();}catch(e){} "
                "for(const w in (PrimeFaces.widgets||{})){try{if(/dlgModalArquivos/i.test(w))PrimeFaces.widgets[w].hide();}catch(e){}}}")
            await page.wait_for_timeout(300)
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(200)
    return anexados


# ---------------------------------------------------------------------------
# Leitura estruturada da caixa de entrada do usuário
# ---------------------------------------------------------------------------
async def listar_caixa_estruturada() -> dict:
    """Lê a caixa de entrada do usuário por linha (tabela do usuário), com id_tramite
    (data-rk), id (id_processo), paj, assistido, descrição e linha_texto (todas as
    células). Retorna {ok, itens}."""
    await _ensure_logged_in()
    page = await _get_page()
    await page.goto(f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml",
                    wait_until="domcontentloaded", timeout=30000)
    await _wait_pf_ajax(page, timeout=10000)
    await page.wait_for_timeout(800)
    if not await _resolver_conflito_login(page):
        return {"ok": False, "detalhe": _MSG_CONFLITO_SESSAO, "itens": []}
    dados = await _eval_seguro(page, _JS_CAIXA_USUARIO)
    itens = dados.get("itens", []) if isinstance(dados, dict) else []
    _log.info("listar_caixa_estruturada: %d item(ns) na caixa do usuário.", len(itens))
    return {"ok": True, "itens": itens}


# Extração do DETALHAMENTO do PAJ (reusa os seletores do sincronizador): CPF na tabela
# "Assistido(s) Pessoa(s) Física" (3ª coluna), pretensão/assistido por regex no innerText
# e a narrativa (HTML) do painel `narrativaPanel`.
_JS_DETALHE_PAJ = r"""() => {
  const r = {};
  const body = document.body.innerText;
  const am = body.match(/Assistido\(s\)[\t\s]+([^\n]+)/);
  if (am) r.assistido = am[1].trim();
  const cpfCell = document.querySelector('#detalhamentoForm\\:dataTableListaAssistidosPF tbody tr td:nth-child(3)');
  if (cpfCell) {
    const m = (cpfCell.textContent || '').match(/\d{3}\.\d{3}\.\d{3}-\d{2}/);
    if (m) r.cpf = m[0];
  }
  const pm = body.match(/Pretens[ãa]o[\t\s]+([^\n]+)/);
  if (pm) r.pretensao = pm[1].trim();
  const np = document.querySelector('#detalhamentoForm\\:narrativaPanel');
  if (np) r.narrativa = np.innerHTML;
  return r;
}"""


async def detalhes_paj(url_detalhe: str) -> dict:
    """Abre o detalhamento do PAJ (na MESMA sessão headless) e extrai
    {cpf, pretensao, assistido, narrativa}. Usado como FALLBACK quando o PAJ ainda não
    foi sincronizado localmente (sem metadata.json). Retorna {} em falha/conflito."""
    if not url_detalhe:
        return {}
    await _ensure_logged_in()
    page = await _get_page()
    try:
        await page.goto(url_detalhe, wait_until="domcontentloaded", timeout=30000)
        await _wait_pf_ajax(page, timeout=12000)
        await page.wait_for_timeout(600)
        if not await _resolver_conflito_login(page):
            return {}
        dados = await _eval_seguro(page, _JS_DETALHE_PAJ)
        return dados if isinstance(dados, dict) else {}
    except Exception as e:  # noqa: BLE001
        _log.warning("detalhes_paj(%s): %r", url_detalhe, e)
        return {}


# ---------------------------------------------------------------------------
# MOVIMENTAR
# ---------------------------------------------------------------------------
async def movimentar_paj(
    pajs: list[dict],
    descricao: str,
    fase: str,
    anexos: list[dict] | None = None,
    tipo_arquivo: str = "Documentos do Assistido",
    dry_run: bool = False,
    descricao_html: bool = False,
) -> dict:
    """Registra uma MOVIMENTAÇÃO (novo sequencial) em um ou vários PAJs.
    pajs: [{"id": <id_processo>, "numero": "AAAA/UUU-NNNNN"}, ...] (bloco = mesma unidade).
    Retorna {ok, status, detalhe, pajs, anexos, mov_seq}."""
    pajs = [p for p in (pajs or []) if p and p.get("id")]
    if not pajs:
        return {"ok": False, "status": "falha", "detalhe": "Nenhum PAJ informado."}
    numeros = [p.get("numero", "") for p in pajs]
    _log.info("movimentar_paj(%s) fase=%r dry=%s", numeros, fase, dry_run)

    await _ensure_logged_in()
    page = await _get_page()
    url = (f"{SISDPU_URL}/pages/movimentacao/movimentaProcesso.xhtml"
           f"?id={pajs[0]['id']}&caixaEntrada=false")
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(400)
    if not await _resolver_conflito_login(page):
        return {"ok": False, "status": "falha", "detalhe": _MSG_CONFLITO_SESSAO}

    for extra in pajs[1:]:
        if extra.get("numero"):
            await _adicionar_paj_por_numero(page, _MOV_FORM, f"{_MOV_FORM}:add", extra["numero"])
    lista = await _ler_pajs_lista(page, f"{_MOV_FORM}:processosSelecionados_input")
    faltando = [n for n in numeros if n and n not in lista]
    if faltando:
        return {"ok": False, "status": "falha",
                "detalhe": (f"PAJ(s) não entraram na lista de movimentação: {faltando} "
                            f"(lista={lista}). Possível restrição de unidade no bloco.")}

    await page.evaluate(_JS_MARCAR_TODAS_FASES)
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(700)
    fr = await page.evaluate(_JS_SELECT_OPT, [f"{_MOV_FORM}:faseId", fase, False])
    if not fr.get("ok"):
        return {"ok": False, "status": "falha",
                "detalhe": f"Fase {fase!r} não encontrada no SISDPU (opções={fr.get('opts')})."}
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(900)
    nfases = await page.eval_on_selector_all(
        _sel(f"{_MOV_FORM}:dataTableFasesSelecionadas") + " tbody tr",
        "els=>els.filter(e=>!/Nenhum registro/i.test(e.innerText)).length")
    if not nfases:
        return {"ok": False, "status": "falha", "detalhe": "Fase não foi adicionada à movimentação."}

    await page.evaluate(_JS_SET_CKEDITOR,
                        [descricao, f"{_MOV_FORM}:editorValue", f"{_MOV_FORM}_editorValue",
                         bool(descricao_html)])

    anexados: list[str] = []
    if anexos and not dry_run:
        anexados = await _anexar_documentos_movimentacao(page, anexos, tipo_arquivo)

    if dry_run:
        n_anexos = len([a for a in (anexos or []) if a])
        return {"ok": True, "status": "dry_run",
                "detalhe": f"[dry_run] Movimentaria {numeros} (fase {fase!r}, {n_anexos} anexo(s)).",
                "pajs": numeros}

    with contextlib.suppress(Exception):
        await page.get_by_role("button", name="Movimentar", exact=True).first.click()
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(900)
    body = await page.evaluate("()=>document.body.innerText||''")
    ok = bool(re.search(r"gravad[oa] com sucesso", body, re.I))
    for d in await _dialogos_visiveis(page):
        if "tramitar" in _norm_nome(d.get("msg", "")).lower():
            with contextlib.suppress(Exception):
                await page.locator(_sel(f"{_MOV_FORM}:confirmDialogTramitacao") + ' button', has_text="Não").first.click()
                await page.wait_for_timeout(600)
            break
    if not ok and "detalhamentoProcesso" in page.url:
        ok = True
    detalhe = "Movimentação registrada com sucesso." if ok else "Não confirmei a movimentação (sem growl de sucesso)."
    if anexados:
        detalhe += f" Anexos: {len(anexados)}."
    mov_seq = await _ler_seq_mov(page, pajs[0]["id"]) if ok else None
    if ok and mov_seq is not None:
        detalhe += f" · Mov {mov_seq}"
    return {"ok": ok, "status": "movimentado" if ok else "falha",
            "detalhe": detalhe, "pajs": numeros, "anexos": anexados, "mov_seq": mov_seq}


# ---------------------------------------------------------------------------
# TRAMITAR
# ---------------------------------------------------------------------------
async def tramitar_paj(
    pajs: list[dict],
    grupo: str | None = None,
    usuarios: list[str] | None = None,
    descricao: str | None = None,
    prazo_data: str | None = None,
    prazo_desc: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Registra uma TRAMITAÇÃO (Trâmite Interno) de PAJ(s) para um GRUPO da unidade
    e/ou USUÁRIO(s). Confirma SEM movimentar. Retorna {ok, status, detalhe, pajs}."""
    pajs = [p for p in (pajs or []) if p and p.get("id")]
    if not pajs:
        return {"ok": False, "status": "falha", "detalhe": "Nenhum PAJ informado."}
    if not grupo and not usuarios:
        return {"ok": False, "status": "falha", "detalhe": "Informe um grupo ou usuário(s) destino."}
    numeros = [p.get("numero", "") for p in pajs]
    _log.info("tramitar_paj(%s) grupo=%r usuarios=%s dry=%s", numeros, grupo, usuarios, dry_run)

    await _ensure_logged_in()
    page = await _get_page()
    url = f"{SISDPU_URL}/pages/tramite/tramitaProcesso.xhtml?id={pajs[0]['id']}&idTramite=0"
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(400)
    if not await _resolver_conflito_login(page):
        return {"ok": False, "status": "falha", "detalhe": _MSG_CONFLITO_SESSAO}

    for extra in pajs[1:]:
        if extra.get("numero"):
            await _adicionar_paj_por_numero(page, _TRAM_FORM, f"{_TRAM_FORM}:Adicionar", extra["numero"])
    lista = await _ler_pajs_lista(page, f"{_TRAM_FORM}:pajs_input")
    faltando = [n for n in numeros if n and n not in lista]
    if faltando:
        return {"ok": False, "status": "falha",
                "detalhe": (f"PAJ(s) não entraram na lista de tramitação: {faltando} (lista={lista}).")}

    with contextlib.suppress(Exception):
        await page.evaluate(r"""()=>{const r=document.querySelector('input[name="form_tramite:j_idt125"][value="1"]');
            if(r&&!r.checked){const box=r.closest('.ui-radiobutton').querySelector('.ui-radiobutton-box'); box&&box.click();}}""")
        await _wait_pf_ajax(page)

    destino = []
    if grupo:
        g = await page.evaluate(_JS_SELECT_OPT, [f"{_TRAM_FORM}:grupoDestinatarioLocal", grupo, False])
        if not g.get("ok"):
            return {"ok": False, "status": "falha",
                    "detalhe": f"Grupo {grupo!r} não encontrado (opções={g.get('opts')})."}
        destino.append(f"grupo {g['txt']!r}")
        await _wait_pf_ajax(page)
        await page.wait_for_timeout(500)
    for nome in (usuarios or []):
        with contextlib.suppress(Exception):
            item = page.locator(
                _sel(f"{_TRAM_FORM}:pickList") + ' .ui-picklist-source li').filter(has_text=nome).first
            await item.dblclick(timeout=5000)
            await _wait_pf_ajax(page)
        destino.append(f"usuário {nome!r}")

    if descricao:
        with contextlib.suppress(Exception):
            await page.fill(_sel(f"{_TRAM_FORM}:descTramite"), descricao)
    if prazo_data:
        with contextlib.suppress(Exception):
            await page.fill(_sel(f"{_TRAM_FORM}:dataPrazo_input"), prazo_data)
    if prazo_desc:
        with contextlib.suppress(Exception):
            await page.evaluate(r"""(txt)=>{const ta=[...document.querySelectorAll('textarea')].find(t=>t.getAttribute('maxlength')==='300');
                if(ta){ta.value=txt; ta.dispatchEvent(new Event('input',{bubbles:true}));}}""", prazo_desc)

    if dry_run:
        return {"ok": True, "status": "dry_run",
                "detalhe": f"[dry_run] Tramitaria {numeros} → {', '.join(destino)}.", "pajs": numeros}

    with contextlib.suppress(Exception):
        await page.evaluate(
            "()=>{window.onbeforeunload=null; try{PF('confirmationDialogTramitarProcessoJudicialWidget').show();}catch(e){}}")
    await page.wait_for_timeout(700)
    dlgs = await _dialogos_visiveis(page)
    if not any("movimentar" in _norm_nome(d.get("msg", "")).lower() for d in dlgs):
        with contextlib.suppress(Exception):
            await page.get_by_role("button", name="Tramitar", exact=True).first.click(timeout=5000)
        await page.wait_for_timeout(700)
        dlgs = await _dialogos_visiveis(page)
    if not any("movimentar" in _norm_nome(d.get("msg", "")).lower() for d in dlgs):
        _log.warning("tramitar_paj: diálogo não apareceu; dialogs=%s url=%s", dlgs, page.url)
        return {"ok": False, "status": "falha", "detalhe": "Diálogo de confirmação da tramitação não apareceu."}
    with contextlib.suppress(Exception):
        await page.locator(_sel(f"{_TRAM_FORM}:NaoTramitarProcessoJudicial")).click(timeout=5000)
    await _wait_pf_ajax(page)
    await page.wait_for_timeout(1000)
    body = await page.evaluate("()=>document.body.innerText||''")
    ok = bool(re.search(r"tramitad[oa]s?.*sucesso", body, re.I)) or "historicoTramitacao" in page.url
    detalhe = (f"Tramitação registrada para {', '.join(destino)}." if ok
               else "Não confirmei a tramitação (sem growl de sucesso).")
    return {"ok": ok, "status": "tramitado" if ok else "falha", "detalhe": detalhe, "pajs": numeros}


# ---------------------------------------------------------------------------
# CONCLUIR (admitir na caixa de grupo + concluir na caixa do usuário)
# ---------------------------------------------------------------------------
async def concluir_paj(id_tramites: list[str], dry_run: bool = False) -> dict:
    """ADMITE (retira da caixa de grupo) e CONCLUI (baixa) o(s) trâmite(s) ORIGINÁRIO(S)
    pelo `id_tramite` (data-rk; não muda ao admitir). Retorna
    {ok, status, detalhe, resultados:{id:{admitido,concluido,ja_ausente}}}.

    IMPORTANTE: a checagem final só confirma que o id sumiu da caixa/grupo — isso é
    verdadeiro tanto quando ESTA chamada admitiu+concluiu o trâmite quanto quando ele
    JÁ estava ausente (concluído manualmente por fora, ou id incorreto) ANTES de
    começar. Por isso rastreamos `visto[i]` (o id foi encontrado em algum momento,
    na aba grupo OU na caixa pessoal, ANTES de qualquer ação) para não reportar
    "concluído com sucesso" em cima de um id que nunca existiu nesta execução —
    nesse caso o resultado é `status="ja_ausente"` (nada foi feito agora)."""
    ids = [str(i).strip() for i in (id_tramites or []) if str(i).strip()]
    if not ids:
        return {"ok": False, "status": "falha", "detalhe": "Nenhum id de trâmite informado."}
    _log.info("concluir_paj(%s) dry=%s", ids, dry_run)
    res = {i: {"admitido": False, "concluido": False, "ja_ausente": False} for i in ids}
    if dry_run:
        return {"ok": True, "status": "dry_run",
                "detalhe": f"[dry_run] Admitiria+concluiria os trâmites {ids}.", "resultados": res}

    await _ensure_logged_in()
    page = await _get_page()
    cx_url = f"{SISDPU_URL}/pages/caixaentrada/caixaEntrada.xhtml"

    await page.goto(cx_url, wait_until="domcontentloaded", timeout=30000)
    await _wait_pf_ajax(page); await page.wait_for_timeout(600)
    if not await _resolver_conflito_login(page):
        return {"ok": False, "status": "falha", "detalhe": _MSG_CONFLITO_SESSAO, "resultados": res}
    await page.evaluate(_JS_ATIVA_ABA_GRUPO)
    await _wait_pf_ajax(page); await page.wait_for_timeout(900)
    visto: dict[str, bool] = {i: False for i in ids}
    for i in ids:
        try:
            if await page.evaluate(_JS_TRAMITE_NO_GRUPO, i):
                visto[i] = True
                if await page.evaluate(_JS_ADMITIR_TRAMITE, i):
                    await _wait_pf_ajax(page, timeout=15000); await page.wait_for_timeout(800)
                    res[i]["admitido"] = True
            else:
                res[i]["admitido"] = None
        except Exception as e:  # noqa: BLE001
            _log.warning("concluir_paj: admitir %s falhou: %s", i, e)

    await page.goto(cx_url, wait_until="domcontentloaded", timeout=30000)
    await _wait_pf_ajax(page); await page.wait_for_timeout(700)
    marcados = 0
    for i in ids:
        try:
            achou_caixa = await page.evaluate(_JS_TRAMITE_NA_CAIXA, i)
            if achou_caixa:
                visto[i] = True
                if await page.evaluate(_JS_MARCA_CHECKBOX, i):
                    marcados += 1
        except Exception as e:  # noqa: BLE001
            _log.warning("concluir_paj: marcar %s falhou: %s", i, e)
    if marcados:
        await page.evaluate(_JS_CLICA_BTN_CONCLUIR)
        await _wait_pf_ajax(page, timeout=15000); await page.wait_for_timeout(1300)

    await page.goto(cx_url, wait_until="domcontentloaded", timeout=30000)
    await _wait_pf_ajax(page); await page.wait_for_timeout(600)
    na_caixa = {i: await page.evaluate(_JS_TRAMITE_NA_CAIXA, i) for i in ids}
    await page.evaluate(_JS_ATIVA_ABA_GRUPO)
    await _wait_pf_ajax(page); await page.wait_for_timeout(800)
    no_grupo = {i: await page.evaluate(_JS_TRAMITE_NO_GRUPO, i) for i in ids}
    for i in ids:
        ausente_agora = not na_caixa[i] and not no_grupo[i]
        if ausente_agora and not visto[i]:
            # Nunca foi encontrado (nem na aba grupo, nem na caixa pessoal) em NENHUM
            # momento desta chamada — já estava ausente ANTES de começarmos. Não é uma
            # conclusão desta execução (provável conclusão manual anterior, ou id errado).
            res[i]["concluido"] = False
            res[i]["ja_ausente"] = True
        else:
            res[i]["concluido"] = ausente_agora

    concluidos = [i for i in ids if res[i]["concluido"]]
    ja_ausentes = [i for i in ids if res[i]["ja_ausente"]]
    if len(concluidos) == len(ids):
        status, ok = "concluido", True
        detalhe = f"{len(concluidos)}/{len(ids)} trâmite(s) concluído(s)."
    elif len(ja_ausentes) == len(ids):
        status, ok = "ja_ausente", False
        detalhe = (f"{len(ja_ausentes)}/{len(ids)} trâmite(s) já estava(m) ausente(s) da caixa/grupo "
                   "antes desta execução — provavelmente já concluído(s) manualmente. Nada foi alterado agora.")
    elif concluidos or ja_ausentes:
        status, ok = "parcial", False
        detalhe = (f"{len(concluidos)}/{len(ids)} concluído(s) agora, {len(ja_ausentes)} já ausente(s) "
                   "antes desta execução.")
    else:
        status, ok = "falha", False
        detalhe = f"{len(concluidos)}/{len(ids)} trâmite(s) concluído(s)."
    return {"ok": ok, "status": status, "detalhe": detalhe, "resultados": res}


# ---------------------------------------------------------------------------
# Grupos da unidade (para o dropdown de tramitação / favoritas)
# ---------------------------------------------------------------------------
async def listar_grupos_unidade(id_processo: str) -> list[str]:
    """Lê os nomes das caixas de grupo da unidade a partir do dropdown
    `grupoDestinatarioLocal` da tela de tramitação (precisa de um id de PAJ p/ abrir)."""
    if not id_processo:
        return []
    try:
        await _ensure_logged_in()
        page = await _get_page()
        url = f"{SISDPU_URL}/pages/tramite/tramitaProcesso.xhtml?id={id_processo}&idTramite=0"
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await _wait_pf_ajax(page); await page.wait_for_timeout(400)
        if not await _resolver_conflito_login(page):
            return []
        grupos = await _eval_seguro(page, r"""()=>{
            const s=document.getElementById('form_tramite:grupoDestinatarioLocal');
            return s?[...s.options].map(o=>o.text.trim()).filter(t=>t && !/selecione/i.test(t)):[];
        }""")
        return grupos or []
    except Exception as e:  # noqa: BLE001
        _log.warning("listar_grupos_unidade falhou: %r", e)
        return []


async def listar_fases_movimentacao(id_processo: str) -> list[str]:
    """Lê os nomes das FASES de movimentação disponíveis (dropdown `faseId` da tela
    de movimentação), com 'Todas as fases' marcado. Precisa de um id de PAJ p/ abrir."""
    if not id_processo:
        return []
    try:
        await _ensure_logged_in()
        page = await _get_page()
        url = (f"{SISDPU_URL}/pages/movimentacao/movimentaProcesso.xhtml"
               f"?id={id_processo}&caixaEntrada=false")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await _wait_pf_ajax(page); await page.wait_for_timeout(400)
        if not await _resolver_conflito_login(page):
            return []
        await page.evaluate(_JS_MARCAR_TODAS_FASES)
        await _wait_pf_ajax(page); await page.wait_for_timeout(700)
        js = ("()=>{const s=document.getElementById('%s:faseId');"
              "return s?[...s.options].map(o=>(o.text||'').trim())"
              ".filter(t=>t && !/selecione/i.test(t)):[];}" % _MOV_FORM)
        fases = await _eval_seguro(page, js)
        return fases or []
    except Exception as e:  # noqa: BLE001
        _log.warning("listar_fases_movimentacao falhou: %r", e)
        return []


# Lê a tela "Histórico de Tramitações do PAJ" e devolve as tramitações AINDA ABERTAS
# (sem "Data / Hora de Conclusão"): o(s) grupo/usuário que está(ão) com o PAJ agora.
_JS_CAIXAS_ABERTAS = r"""() => {
  for (const t of document.querySelectorAll('table')) {
    const ths = [...t.querySelectorAll('thead th, thead td')].map(x => (x.innerText||'').trim());
    if (!ths.length) continue;
    const iConcl = ths.findIndex(h => /conclus/i.test(h));
    const iGrupo = ths.findIndex(h => /grupo/i.test(h));
    const iDest  = ths.findIndex(h => /destinat/i.test(h));
    const iRem   = ths.findIndex(h => /remetente/i.test(h));
    const iData  = ths.findIndex(h => /tr[âa]mite/i.test(h));
    const iDesc  = ths.findIndex(h => /descri/i.test(h));
    if (iConcl < 0 || (iGrupo < 0 && iDest < 0)) continue;
    const abertas = [];
    for (const tr of t.querySelectorAll('tbody tr')) {
      const tds = [...tr.querySelectorAll('td')];
      if (!tds.length) continue;
      const cell = i => (i >= 0 && tds[i] ? (tds[i].innerText||'').trim() : '');
      if (cell(iConcl)) continue;  // já concluído -> não está aberto
      const grupo = cell(iGrupo), dest = cell(iDest);
      const local = grupo || dest;
      if (!local) continue;
      abertas.push({ local, grupo, destinatario: dest,
                     remetente: cell(iRem), data: cell(iData), descricao: cell(iDesc) });
    }
    return abertas;
  }
  return [];
}"""


async def caixas_abertas_paj(id_processo: str) -> list[dict]:
    """Onde o PAJ está ABERTO agora: linhas da tela 'Histórico de Tramitações do PAJ'
    sem data de conclusão. Retorna [{local, grupo, destinatario, remetente, data, descricao}]."""
    if not id_processo:
        return []
    try:
        await _ensure_logged_in()
        page = await _get_page()
        url = f"{SISDPU_URL}/pages/tramite/historicoTramitacao.xhtml?id={id_processo}"
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await _wait_pf_ajax(page); await page.wait_for_timeout(800)
        if not await _resolver_conflito_login(page):
            return []
        res = await _eval_seguro(page, _JS_CAIXAS_ABERTAS)
        return res if isinstance(res, list) else []
    except Exception as e:  # noqa: BLE001
        _log.warning("caixas_abertas_paj falhou: %r", e)
        return []
