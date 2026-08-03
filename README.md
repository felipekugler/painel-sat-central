# Painel SAT Central

Painel autônomo com a feature **"Arquivos SAT"**: baixa os documentos
previdenciários de um assistido no SAT Central (INSS/Dataprev) e os anexa a um
PAJ do SISDPU (movimentação → tramitação).

A única diferença em relação à feature original do PAINEL SISDPU é que os PAJs
são **incluídos manualmente** (por número — o painel busca os dados no SISDPU
via busca global) em vez de detectados automaticamente na caixa de entrada.
Por isso não há **conclusão** de trâmite: como o PAJ não vem de um trâmite da
caixa, não existe um trâmite originário para concluir — o fluxo termina na
tramitação.

## Como usar

1. Crie o ambiente virtual e instale as dependências:

   ```bash
   py -3 -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   .venv\Scripts\python -m playwright install chromium
   ```

2. (Opcional) copie `.env.example` para `.env` e ajuste `SAT_DADOS_DIR` se
   quiser que os PDFs e os arquivos de estado fiquem em outro lugar. Por
   padrão ficam na pasta `dados/`, irmã desta pasta `Projeto/` (mesmo padrão
   dos demais painéis DPU — nada é salvo no Desktop).

3. Dê duplo clique em `Painel-SAT-Central.bat` (ou rode `python app.py`) e
   acesse `http://127.0.0.1:8003/`.

4. Em **Configurações**, cadastre as credenciais do SISDPU no **Cofre de
   credenciais**: primeiro cadastro cria uma senha-mestra (mín. 6 caracteres)
   que cifra usuário/senha em disco — ela não é gravada em lugar nenhum e vale
   só na sessão atual (desbloqueie de novo a cada reinício do painel).

5. Na tela principal: em **Adicionar PAJ**, informe o número (AAAA/UUU-NNNNN)
   — o painel pede a senha-mestra (se ainda trancada) e busca os dados (CPF,
   assistido, pretensão) no SISDPU. Clique em **Abrir SAT / Login** e faça o
   login (usuário/senha/2FA) e o CAPTCHA na janela que abrir. Depois clique em
   **Baixar arquivos** e, com os documentos baixados, **Anexar ao PAJ**
   (movimenta e tramita para a caixa escolhida).

Para encerrar o servidor, use o botão **Encerrar painel** na tela, ou rode
`python stop.py`.

## Configurações

A página **Configurações** tem:

- **Cofre de credenciais** — cadastro/troca da senha-mestra e do usuário/senha
  do SISDPU (cifrados em disco por Fernet/PBKDF2). O desbloqueio é sob
  demanda: só pede a senha-mestra quando alguma ação realmente precisa do
  SISDPU (adicionar PAJ, movimentar/tramitar, reconectar).
- **Arquivos SAT Central** — valores padrão de tramitação/consulta: caixa de
  tramitação padrão, prazo de conclusão da tramitação, tempo entre consultas
  ao SAT, espera após bloqueio anti-robô e compilação de PDFs por tipo (quando
  o número de documentos do mesmo tipo passa de um limiar).

Os **tipos de documento a baixar por pretensão** e as **espécies de PAT a
ignorar** ficam nos botões "Escolher documentos" e "Ignorar", na própria aba
principal (mesmo padrão do PAINEL SISDPU).

## Estrutura

```
Painel SAT Central/
├── Projeto/    código (este README) + .venv — tudo que roda o painel
└── dados/      PAJs/<paj_norm>/Arquivos SAT/..., cofre cifrado, perfil do SAT,
                 PAJs manuais, histórico — nunca no Desktop
```

Dentro de `Projeto/`:

```
app.py                        servidor FastAPI (porta 8003)
config.py                     pasta de dados (padrão: ../dados, ajustável via SAT_DADOS_DIR)
routes/sat.py                 rotas (página + API: download, mov/tram, config, segurança)
routes/cofre.py                rotas do cofre de credenciais (senha-mestra)
services/sat_service.py       PAJs manuais, pastas, inventário, mov/tram (sem conclusão)
services/config_service.py    configurações (tipos/PAT/ritmo/tramitação)
services/historico_sat.py     log JSONL dos PAJs trabalhados
services/conexao_service.py   estado da conexão com o SISDPU (selo da barra superior)
services/cofre_service.py     cofre cifrado (Fernet/PBKDF2) — senha-mestra única
ingestao/sat_client.py        motor de download do SAT (Playwright via CDP)
ingestao/sat_utils.py         sanitização de nomes, dedup do PAT, compilação de PDFs
ingestao/sisdpu_client.py     login/sessão do SISDPU + busca global de PAJ
ingestao/sisdpu_movtram.py    movimentar / tramitar / listar_grupos_unidade
templates/                    UI (Tailwind + DaisyUI + Alpine.js)
```
