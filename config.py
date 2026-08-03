"""Configuracao central do Painel SAT Central — painel autonomo com a funcao
"Arquivos SAT": baixar os documentos do SAT Central (INSS/Dataprev) e anexa-los
a um PAJ do SISDPU (movimentacao -> tramitacao). Os PAJs sao incluidos
MANUALMENTE (busca global no SISDPU por numero) — nao ha varredura automatica
da caixa de entrada.

As credenciais do SISDPU vivem no COFRE cifrado por senha-mestra
(services.cofre_service), nao aqui — nao ha fallback via .env.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# Pasta raiz onde ficam os dados do painel: perfil do navegador do SAT (sessao/
# cookies), o cofre de credenciais cifrado, a pasta de cada PAJ (com os PDFs
# baixados) e os arquivos de estado (PAJs manuais, historico). Mesmo padrao
# dos demais paineis DPU: pasta "dados" IRMA da pasta "Projeto" (onde este
# arquivo vive) — nao no Desktop. Ajustavel no .env (SAT_DADOS_DIR) se
# precisar apontar para outro lugar.
BASE_DIR = Path(__file__).resolve().parent  # .../Painel SAT Central/Projeto
OFICIO_GERAL = Path(os.getenv("SAT_DADOS_DIR", str(BASE_DIR.parent / "dados")))
PAJS_DIR = OFICIO_GERAL / "PAJs"
