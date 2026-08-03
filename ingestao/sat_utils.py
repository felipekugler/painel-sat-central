#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Utilitários da skill SAT-DownloadArquivos.

Funções:
- sanitize(nome): remove acentos, () [] e caracteres especiais; preserva pontos
  (NB) e traços (datas); colapsa espaços.
- abreviar(texto): substitui espécie/serviço completo pelas siglas (BPC, BPC
  DEFICIENTE, BPC IDOSO, BI TEMPORARIO, etc.), tolerante a acentos.
- nome_final(base): abreviar + sanitize.
- extrair_campos_procadm(pdf_path): {prot, nb, der, paginas, subtarefas[]}.

CLI:
  python sat_utils.py pat <pasta_temp> <pasta_destino> <mapa_json>
    mapa_json: { "arquivo_temp.pdf": {"servico": "...", "protocolo": "<prot da linha>"} }
    Deduplica por (PROTOCOLO DE REQUERIMENTO interno + nº de páginas), mantendo a
    tarefa PRINCIPAL (linha cujo protocolo == prot interno), aplica nome PROCADM,
    sanitiza/abrevia e MOVE os únicos para a pasta destino. Imprime relatório.

  python sat_utils.py nome "<texto>"      -> testa abreviar+sanitize
  python sat_utils.py campos <pdf>        -> imprime campos PROCADM de um PDF
"""
import sys, os, re, json, shutil, unicodedata, datetime, contextlib

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

# ---- Abreviação de espécie / serviço (ordem: mais específico primeiro) ----
_ABREV = [
    (r'BENEFICIO DE PRESTACAO CONTINUADA A PESSOA COM DEFICIENCIA', 'BPC DEFICIENTE'),
    (r'BENEFICIO DE PRESTACAO CONTINUADA (?:AO IDOSO|A PESSOA IDOSA)', 'BPC IDOSO'),
    (r'BENEFICIO DE PRESTACAO CONTINUADA', 'BPC'),
    (r'BENEFICIO ASSISTENCIAL A PESSOA COM DEFICIENCIA', 'BPC DEFICIENTE'),
    (r'BENEFICIO ASSISTENCIAL AO IDOSO', 'BPC IDOSO'),
    (r'BENEFICIO ASSISTENCIAL', 'BPC'),
    (r'AUXILIO POR INCAPACIDADE TEMPORARIA ACIDENTARIO', 'BI TEMPORARIO ACIDENTARIO'),
    (r'AUXILIO POR INCAPACIDADE TEMPORARIA PREVIDENCIARIO', 'BI TEMPORARIO'),
    (r'APOSENTADORIA POR INCAPACIDADE PERMANENTE', 'APOSENTADORIA POR INCAPACIDADE'),
]


def _noacc(s):
    return ''.join(c for c in unicodedata.normalize('NFD', s)
                   if unicodedata.category(c) != 'Mn')


def abreviar(texto):
    """Aplica as abreviações de espécie/serviço (sobre texto sem acento)."""
    out = _noacc(texto)
    for pat, ab in _ABREV:
        out = re.sub(pat, ab, out, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', out).strip()


def sanitize(base):
    """Sem acentos, sem () [], sem caracteres especiais; mantém pontos e traços."""
    n = _noacc(base)
    n = n.replace('(', '').replace(')', '').replace('[', '').replace(']', '')
    n = re.sub(r'[^\w\s\-.]', ' ', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n


def nome_final(base):
    """Nome pronto para arquivo (sem extensão): abreviar + sanitize."""
    return sanitize(abreviar(base))


# ---- Extração de campos do PDF de processo (GET - Gerenciador de Tarefas) ----
def _fmt_nb(raw):
    return f'{raw[0:3]}.{raw[3:6]}.{raw[6:9]}-{raw[9]}'


def _achar_nb(t):
    # rótulo NB -> valor pontuado (10) ou cru (10); (?!\d) evita o CPF (11 díg.)
    m = re.search(r'\bNB\b\s*[:\-]?\s*\n?\s*(\d{3}\.\d{3}\.\d{3}-\d|\d{10})(?!\d)', t, re.I)
    if m:
        v = m.group(1)
        return v if '.' in v else _fmt_nb(v)
    m = re.search(r'(\d{3}\.\d{3}\.\d{3}-\d)(?!\d)', t)  # fallback pontuado, não-CPF
    return m.group(1) if m else ''


def extrair_campos_procadm(pdf_path):
    if fitz is None:
        raise RuntimeError('PyMuPDF (fitz) não instalado.')
    d = fitz.open(pdf_path)
    paginas = d.page_count
    full = '\n'.join(d.load_page(i).get_text() for i in range(paginas))
    d.close()
    pm = re.search(r'PROTOCOLO\s+DE\s+REQUERIMENTO\s*[\n\s]*(\d{6,})', full, re.I | re.M)
    prot = pm.group(1) if pm else ''
    nb = _achar_nb(full)
    dm = re.search(r'(?:Data\s+de\s+entrada|Entrada)[:\s]+(\d{2}/\d{2}/\d{4})', full, re.I)
    der = '-'.join(dm.group(1).split('/')) if dm else ''
    subs = re.findall(r'(\d{6,})\s*-\s*[^\n]*\(Subtarefa\)', full)
    return {'prot': prot, 'nb': nb, 'der': der, 'paginas': paginas,
            'subtarefas': list(dict.fromkeys(subs))}


# ---- Regra: não baixar PAT (processo administrativo) com mais de 5 anos ----
PAT_MAX_ANOS = 5


def _der_para_data(der):
    """Converte o DER ('DD-MM-AAAA', como extraído de extrair_campos_procadm) para
    datetime.date. Retorna None se vazio/inválido."""
    if not der:
        return None
    try:
        return datetime.datetime.strptime(der, '%d-%m-%Y').date()
    except ValueError:
        return None


def _mais_de_5_anos(d, ref=None):
    """True se a Data de entrada `d` (date) tiver MAIS de PAT_MAX_ANOS anos em relação
    a `ref` (hoje por padrão). Sem data (None) NÃO bloqueia — não há como afirmar."""
    if d is None:
        return False
    ref = ref or datetime.date.today()
    try:
        corte = ref.replace(year=ref.year - PAT_MAX_ANOS)
    except ValueError:  # 29/02 em ano não bissexto -> 28/02
        corte = ref.replace(year=ref.year - PAT_MAX_ANOS, day=28)
    return d < corte  # anterior ao corte = protocolo com mais de 5 anos


def nb_digits(texto):
    """Extrai só os dígitos do 1º NB (NNN.NNN.NNN-N) achado no texto; '' se não houver.
    Usado para comparar/ordenar NBs vindos de nomes de arquivo ou de campos do PDF."""
    m = re.search(r'\d{3}\.\d{3}\.\d{3}-\d', texto or '')
    return re.sub(r'\D', '', m.group(0)) if m else ''


def merge_pdfs(paths, destino):
    """Une os PDFs de `paths` (NA ORDEM dada) num único arquivo `destino`.
    Retorna o nº de páginas do resultado. Requer PyMuPDF (fitz)."""
    if fitz is None:
        raise RuntimeError('PyMuPDF (fitz) não instalado.')
    out = fitz.open()
    try:
        for p in paths:
            with fitz.open(p) as d:
                out.insert_pdf(d)
        out.save(destino)
        return out.page_count
    finally:
        out.close()


def _nome_procadm(servico, campos, prefixo=''):
    nb, der, prot = campos['nb'], campos['der'], campos['prot']
    if servico and nb and der and prot:
        base = f'PROCADM {servico} - NB {nb} DER {der} PROT {prot}'
    elif servico and der and prot:
        base = f'PROCADM {servico} - DER {der} PROT {prot}'
    else:
        base = f'PROCADM {servico} - PROT {prot}'
    return nome_final(prefixo + base) + '.pdf'


def processar_pat(pasta_temp, pasta_dest, mapa, prefixo='', nbs=None,
                  aplicar_regra_5anos=True):
    """mapa: {arquivo_temp: {'servico':..., 'protocolo': <prot da linha>}}. `prefixo`
    é anteposto ao nome final (ex.: 'INSTITUIDOR - '). `nbs` = conjunto de NBs (só
    dígitos) indicados pelo usuário; se fornecido, processos cujo NB não esteja no
    conjunto (inclusive sem NB) são pulados. `aplicar_regra_5anos` = quando False, NÃO
    descarta por antiguidade (usado em pedidos EXPLÍCITOS/complementares, orientados pelo
    usuário, que devem baixar independentemente da idade).

    Retorna (mantidos, descartados, pulados_prazo, pulados_nb):
    - mantidos:      [(arquivo_temp, nome_final)] movidos para a pasta destino;
    - descartados:   [(arquivo_temp, motivo)] subtarefas/duplicatas removidas;
    - pulados_prazo: [(arquivo_temp, motivo)] protocolos com mais de 5 anos (Data de
                     entrada), NÃO baixados por regra;
    - pulados_nb:    [(arquivo_temp, motivo)] processos de NB não indicado pelo usuário.
    """
    info = {}
    for f in mapa:
        p = os.path.join(pasta_temp, f)
        if os.path.exists(p):
            info[f] = extrair_campos_procadm(p)

    # agrupar por (prot interno, paginas)
    grupos = {}
    for f, c in info.items():
        grupos.setdefault((c['prot'], c['paginas']), []).append(f)

    def _apagar(arquivos):
        for a in arquivos:
            with contextlib.suppress(OSError):
                os.remove(os.path.join(pasta_temp, a))

    mantidos, descartados, pulados_prazo, pulados_nb = [], [], [], []
    for chave, arquivos in grupos.items():
        prot_int = chave[0]
        # principal = linha cujo protocolo == prot interno; senão o primeiro
        principal = next((a for a in arquivos
                          if str(mapa[a].get('protocolo', '')) == prot_int), arquivos[0])
        servico = mapa[principal].get('servico', '')
        campos = info[principal]

        # Regra: não baixar processo administrativo (PAT) com mais de 5 anos,
        # considerando a Data de entrada do protocolo. Descarta o grupo inteiro
        # (principal + subtarefas) e registra o motivo para o log. NÃO se aplica a
        # pedidos explícitos/complementares (aplicar_regra_5anos=False).
        der = campos.get('der', '')
        if aplicar_regra_5anos and _mais_de_5_anos(_der_para_data(der)):
            _apagar(arquivos)
            pulados_prazo.append((principal,
                                  f'protocolo {prot_int} ({servico}) — pulado - protocolo há '
                                  f'mais de 5 anos (Data de entrada {der.replace("-", "/")})'))
            continue

        # Filtro por NB indicado pelo usuário: pula processos de NB não indicado
        # (inclusive os SEM NB, quando o filtro está ativo).
        if nbs:
            nbd = nb_digits(campos.get('nb', ''))
            if nbd not in nbs:
                _apagar(arquivos)
                nb_txt = campos.get('nb', '') or 'sem NB'
                pulados_nb.append((principal,
                                   f'protocolo {prot_int} ({servico}) — pulado - NB {nb_txt} '
                                   f'não indicado pelo usuário'))
                continue

        nome = _nome_procadm(servico, campos, prefixo)
        shutil.move(os.path.join(pasta_temp, principal), os.path.join(pasta_dest, nome))
        mantidos.append((principal, nome))
        for a in arquivos:
            if a != principal:
                # Best-effort: se a exclusão da subtarefa/duplicata falhar (ex.: bloqueio
                # de antivírus/EDR no delete), NÃO aborta o processamento dos demais
                # protocolos — o arquivo só fica órfão em pasta_temp (removido no início
                # da próxima rodada de PAT) em vez de derrubar toda a deduplicação.
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(pasta_temp, a))
                descartados.append((a, f'protocolo {mapa[a].get("protocolo","?")} '
                                       f'descartado — é subtarefa/duplicata do protocolo '
                                       f'principal {prot_int} ({servico})'))
    return mantidos, descartados, pulados_prazo, pulados_nb


def _cli():
    if len(sys.argv) < 2:
        print(__doc__); return
    cmd = sys.argv[1]
    if cmd == 'nome':
        print(nome_final(sys.argv[2]))
    elif cmd == 'campos':
        print(json.dumps(extrair_campos_procadm(sys.argv[2]), ensure_ascii=False, indent=2))
    elif cmd == 'pat':
        pasta_temp, pasta_dest, mapa_json = sys.argv[2], sys.argv[3], sys.argv[4]
        with open(mapa_json, encoding='utf-8') as fh:
            mapa = json.load(fh)
        mant, desc, pul_prazo, pul_nb = processar_pat(pasta_temp, pasta_dest, mapa)
        print('=== MANTIDOS (principais/únicos) ===')
        for _, nome in mant:
            print('  [OK]', nome)
        print('=== DESCARTADOS ===')
        for a, motivo in desc:
            print('  [x]', a, '->', motivo)
        print('=== PULADOS (protocolo há mais de 5 anos) ===')
        for a, motivo in pul_prazo:
            print('  [->]', a, '->', motivo)
        print('=== PULADOS (NB não indicado) ===')
        for a, motivo in pul_nb:
            print('  [->]', a, '->', motivo)
    else:
        print(__doc__)


if __name__ == '__main__':
    _cli()
