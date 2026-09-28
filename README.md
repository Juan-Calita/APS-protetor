# Pipeline de Ectoscopia Clínica — Veo 3.1 → FFmpeg → Google Drive → Planilha

Serviço autônomo em Python que produz, de ponta a ponta e sem intervenção
manual, a matriz completa de **4 personas × 5 estados físicos = 20 clipes**
de ectoscopia clínica para uso educacional.

Cada execução:

1. **Gera** o vídeo no Google Veo 3.1 (`google-genai`), com polling assíncrono,
   backoff exponencial e fallback automático de parâmetros.
2. **Pós-processa** com FFmpeg: loop *seamless* por crossfade, recorte 1:1 em
   720×720, 24 fps, H.264 Main, `yuv420p`, sem áudio, `+faststart`.
3. **Extrai** o pôster JPG do primeiro frame e calcula o **SHA-256** do MP4 e do JPG.
4. **Organiza e envia** para `medical-assets/ectoscopia/<Ator>/` no Google Drive.
5. **Atualiza em tempo real** a planilha de controle (Excel ou Google Sheets)
   com status, tamanho, links do Drive, hash e data/hora.

---

## Índice

- [Instalação](#instalação)
- [Configuração](#configuração)
- [Uso](#uso)
- [Arquitetura](#arquitetura)
- [O elenco e a matriz de estados](#o-elenco-e-a-matriz-de-estados)
- [Como o loop *seamless* funciona](#como-o-loop-seamless-funciona)
- [Notas técnicas importantes](#notas-técnicas-importantes)
- [Resiliência e retomada](#resiliência-e-retomada)
- [Testes](#testes)
- [Solução de problemas](#solução-de-problemas)

---

## Instalação

```bash
git clone <este-repositório>
cd APS-protetor

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**Dependência externa obrigatória — FFmpeg ≥ 5.0** (`ffmpeg` e `ffprobe` no `PATH`):

| Sistema        | Comando                            |
| -------------- | ---------------------------------- |
| Debian/Ubuntu  | `sudo apt-get install -y ffmpeg`   |
| macOS          | `brew install ffmpeg`              |
| Windows        | `winget install Gyan.FFmpeg`       |

---

## Configuração

```bash
cp .env.example .env
```

Preencha o `.env`. O mínimo para gerar vídeos é `GOOGLE_API_KEY`.

### 1. Chave do Veo

Obtenha em <https://aistudio.google.com/apikey> e defina `GOOGLE_API_KEY`.

> A geração de vídeo no Veo é um recurso **pago**. 20 clipes consomem cota real.
> Use `--dry-run` para validar todo o pipeline sem gastar um centavo.

### 2. Google Drive

Escolha um modo de autenticação:

**Service Account** (recomendado para automação/cron) — `DRIVE_AUTH_MODE=service_account`

1. No Google Cloud Console, crie uma Service Account e baixe a chave JSON como
   `service_account.json`.
2. Ative a **Google Drive API** no projeto.
3. ⚠️ **Service Accounts não possuem cota de armazenamento própria no Drive.**
   Um upload para o "Meu Drive" delas falha com `storageQuotaExceeded`.
   Portanto, defina **uma** das opções:
   - `DRIVE_ROOT_PARENT_ID` — id de uma pasta do *seu* Meu Drive compartilhada
     com o e-mail da Service Account como **Editor**. O id está na URL:
     `https://drive.google.com/drive/folders/<ESTE_É_O_ID>`
   - `DRIVE_SHARED_DRIVE_ID` — id de um Drive compartilhado onde a SA é membro.

**OAuth de usuário** (uso interativo) — `DRIVE_AUTH_MODE=oauth`

Baixe o `client_secret.json` (tipo *Desktop app*). Na primeira execução o
navegador abre para consentimento; o token fica cacheado em `drive_token.json`.

### 3. Planilha de controle

| Backend  | Configuração                                                      |
| -------- | ----------------------------------------------------------------- |
| `excel`  | *(padrão)* grava `controle_producao_ectoscopia.xlsx` em disco.     |
| `sheets` | `SHEETS_SPREADSHEET_ID` + planilha compartilhada com a SA (Editor). |
| `none`   | Desativa o registro.                                              |

Se o Google Sheets falhar na conexão, o pipeline **degrada automaticamente
para Excel** em vez de abortar a produção.

---

## Uso

```bash
# Smoke test: valida FFmpeg, manifesto e planilha SEM chamar o Veo
python run_pipeline.py --dry-run --skip-drive

# Produção completa: 20 clipes
python run_pipeline.py

# Subconjuntos
python run_pipeline.py --actors A01 A03
python run_pipeline.py --states basal dispneia
python run_pipeline.py --actors A02 --states dor_intensa --force

# Inspeção (não gera nada)
python run_pipeline.py --list
python run_pipeline.py --print-prompt A03 dispneia
```

### Opções

| Flag                         | Efeito                                                       |
| ---------------------------- | ------------------------------------------------------------ |
| `--actors A01 …`             | Restringe as personas (padrão: todas).                       |
| `--states basal …`           | Restringe os estados (padrão: todos).                        |
| `--dry-run`                  | Sintetiza clipes locais; não chama o Veo.                    |
| `--skip-drive`               | Não autentica nem envia nada ao Drive.                       |
| `--force`                    | Regera mesmo os clipes já concluídos.                        |
| `--fail-fast`                | Aborta na primeira falha (padrão: continua e reporta ao fim). |
| `--tracker excel\|sheets\|none` | Escolhe o backend da planilha.                            |
| `--work-dir DIR`             | Diretório de trabalho (padrão: `build/`).                    |
| `--print-prompt ATOR ESTADO` | Imprime o prompt composto, para revisão clínica.             |
| `--list`                     | Mostra a matriz completa.                                    |
| `--log-level DEBUG`          | Verbosidade.                                                  |

**Códigos de saída:** `0` sucesso · `1` houve falhas · `2` configuração inválida · `130` interrompido.

### Saídas

```
build/
├── raw/                          # MP4 brutos do Veo (rastreabilidade)
├── logs/pipeline.log
└── out/
    ├── A01/ basal.mp4  basal.jpg  …  manifest.json
    ├── A02/ …
    ├── A03/ …
    └── A04/ …

controle_producao_ectoscopia.xlsx
```

No Drive, espelhado em `medical-assets/ectoscopia/A01..A04/`.

---

## Arquitetura

| Módulo             | Responsabilidade                                                    |
| ------------------ | ------------------------------------------------------------------- |
| `run_pipeline.py`  | Orquestração, CLI, manifesto, retomada, progresso, resumo.          |
| `generator.py`     | Veo 3.1: disparo assíncrono, polling, download, fallbacks.          |
| `processor.py`     | FFmpeg: loop por crossfade, encode, pôster, SHA-256.                |
| `drive_manager.py` | Autenticação, criação idempotente de pastas, upload, `webViewLink`. |
| `tracker.py`       | Planilha em tempo real (Excel / Google Sheets).                     |
| `prompts_data.py`  | Elenco, matriz de estados e composição dos prompts.                 |
| `config.py`        | Configuração por ambiente + validação antecipada.                   |
| `utils.py`         | Logging, retentativa com backoff, barra de progresso.               |
| `test_pipeline.py` | 19 testes de fumaça, sem rede e sem credenciais.                    |

---

## O elenco e a matriz de estados

| ID    | Persona                                    | Cenário    | Vieses clínicos                       |
| ----- | ------------------------------------------ | ---------- | ------------------------------------- |
| `A01` | Homem, 58a, pardo, robusto, grisalho       | PS         | dor torácica · DPOC · abdome agudo    |
| `A02` | Mulher, 34a, cabelo escuro preso           | PS         | cefaleia · apendicite · ginecologia   |
| `A03` | Mulher idosa negra, 78a, leito a 30°       | Enfermaria | sepse · IC · delirium                 |
| `A04` | Homem jovem, 24a, pardo, atlético          | PS         | asma · trauma leve · intoxicação      |

**Estados:** `basal` · `desconforto_leve` · `dor_intensa` · `dispneia` · `rebaixamento`

### Regras clínicas inegociáveis

Aplicadas a **todos** os 20 prompts e reforçadas pelo *negative prompt*:

1. **Boca estritamente fechada em todos os frames.** Zero fala, zero lip-sync.
   A frequência respiratória é lida apenas pelo tórax e pelos ombros.
2. **Câmera fixa em tripé.** Sem pan, tilt, zoom, dolly ou handheld.
3. **Nenhum sinal patognomônico.** Sem icterícia, cianose marcada, exantema,
   equimose, assimetria facial ou desvio de rima.
4. **Nenhum equipamento revelador.** Sem máscara de O₂, nebulizador, monitor,
   acesso venoso ou colar cervical.

A persona `A03` recebe um *override* postural, já que está deitada: direções de
"sentar" e "inclinar-se para frente" são traduzidas para cabeça, pescoço,
ombros e mãos sobre o cobertor.

Os prompts são escritos **em inglês** — o Veo 3.x adere de forma
mensuravelmente mais fiel — enquanto rótulos, descrições e metadados
permanecem em português para alimentar planilha e manifesto.

---

## Como o loop *seamless* funciona

O "Plano B" especificado, que elimina o congelamento de frame típico das
abordagens ingênuas de loop:

```
Clipe bruto do Veo (≥ 4 s)
  0s        2s        4s
  ├─────────┼─────────┤
  │    B    │    A    │
            └────┬────┘
  ┌──────────────┘
  ▼
  A = [2s, 4s)   ──┐
                   ├─ xfade(fade, duration=0.5s, offset=1.5s)
  B = [0s, 2s)   ──┘
  ▼
  Saída: 3,5 s  (= 2 × 2 − 0,5)
```

A saída **começa** no instante-fonte 2,0 s e **termina** no instante-fonte
2,0 s (fim de B). Ao repetir, o último frame encosta naturalmente no primeiro.

**Verificação empírica.** Medindo o PSNR entre frames adjacentes de um clipe
com movimento contínuo:

| Transição                         | PSNR       |
| --------------------------------- | ---------- |
| frame 82 → 83 (interior adjacente) | 41,83 dB   |
| frame 83 → 0 (**emenda do loop**)  | 41,12 dB   |

A emenda é estatisticamente indistinguível de uma transição interna comum —
isto é, o loop não apresenta salto nem congelamento.

### Especificação de saída

| Parâmetro   | Valor                          | Garantido por                          |
| ----------- | ------------------------------ | -------------------------------------- |
| Resolução   | 720 × 720 (1:1)                | `scale=…:increase` + `crop` central     |
| Duração     | ~3,5 s                         | `2 × segmento − crossfade`              |
| Taxa        | 24 fps                         | `-r 24`                                 |
| Codec       | H.264 **Main**, `yuv420p`      | `-profile:v main -pix_fmt yuv420p`      |
| Qualidade   | CRF 27, preset `slow`          | escada adaptativa até CRF 40            |
| Streaming   | `+faststart`                   | `-movflags +faststart`                  |
| Áudio       | **ausente**                    | `-an` (validado nos testes)             |
| Tamanho MP4 | ≤ 600 KB                       | CRF sobe em degraus até caber           |
| Pôster JPG  | ≤ 60 KB, `-q:v 4`              | `-q:v` sobe em degraus até caber        |

Se um clipe não couber no orçamento nem com CRF 40, o pipeline **falha
explicitamente** para aquele clipe em vez de entregar um arquivo fora do
especificado — e segue para o próximo.

---

## Notas técnicas importantes

### O Veo 3.x não aceita proporção 1:1

Esta é uma limitação **real e atual** da API: os valores aceitos são `16:9` e
`9:16`. Não existe `1:1` nativo.

A solução adotada — e a razão de o requisito ser cumprido mesmo assim — é
solicitar **16:9 em 720p** e fazer o *center crop* para 720×720 no FFmpeg. O
arquivo final é 1:1 exato, sem barras e sem distorção. O enquadramento dos
prompts é *medium close-up* justamente para que o recorte central preserve
cabeça e tórax.

Se a API passar a aceitar `1:1`, basta definir `VEO_ASPECT_RATIO=1:1`: o
gerador tenta primeiro o valor pedido e só então percorre a escada de fallback.

### A duração de 5 s não está na grade do Veo 3.1

O Veo 3.1 trabalha com durações discretas (tipicamente **4, 6 e 8 s**). O
default é `VEO_DURATION_SECONDS=6`, e qualquer valor ≥ 4 s satisfaz o corte
`[2s,4s) + [0s,2s)`. Se a API recusar o valor pedido, o gerador desce pela
escada `6 → 8 → 4` automaticamente.

O gerador também distingue **qual** parâmetro foi recusado: um `aspect_ratio`
inválido não é reensaiado com outras durações, economizando cota.

### O áudio nativo do Veo é descartado

O Veo 3.x gera áudio. O `-an` no encode final o remove de forma dura — os
testes verificam que o MP4 entregue não contém stream de áudio algum.

---

## Resiliência e retomada

- **Retentativa com backoff exponencial + jitter** em rate limit (429 /
  `RESOURCE_EXHAUSTED`), 5xx e erros de rede — no Veo *e* no Drive.
  Erros permanentes (ex.: prompt inválido) **não** são repetidos.
- **Isolamento de falhas.** Um clipe que falha marca `❌ Falha` na planilha,
  registra o motivo na coluna `Observação` e o pipeline **continua**. O resumo
  final lista todas as falhas; o código de saída vira `1`.
- **Idempotência.** Reexecutar pula clipes já concluídos (`↩️ Reaproveitado`),
  validando contra o `manifest.json` **e** a presença dos arquivos em disco.
  Se o prompt mudou, o clipe é regerado automaticamente.
- **Uploads idempotentes.** Arquivos são atualizados como nova revisão, mantendo
  o mesmo id e o mesmo `webViewLink` — nunca se criam duplicatas no Drive.
- **Escrita atômica.** O `.xlsx` e o `manifest.json` usam `tempfile` +
  `os.replace`; um Ctrl-C jamais deixa arquivo corrompido.
- **Ctrl-C limpo.** Interromper preserva todo o progresso — basta reexecutar.

### A planilha de controle

As oito colunas obrigatórias, na ordem especificada, mais uma nona opcional:

`Ator` · `Estado` · `Status` · `Tamanho MP4 (KB)` · `Link Google Drive (Vídeo)`
· `Link Google Drive (Pôster)` · `SHA-256` · `Data/Hora Conclusão` · `Observação`

A coluna `Observação` existe para que o motivo de uma falha tenha onde morar
sem contaminar a coluna `SHA-256`, que carrega apenas hashes.

**Status:** `⬜ Pendente` → `⏳ Renderizando` → `✅ Concluído` · `❌ Falha` · `↩️ Reaproveitado`

---

## Testes

```bash
python test_pipeline.py      # standalone, sem dependências extras
pytest -q test_pipeline.py   # se preferir pytest
```

19 testes, sem rede e sem credenciais, cobrindo: integridade da matriz de 20
prompts, presença das quatro regras clínicas em 20/20, ausência de contradição
postural (direções de estado que digam "sentado" quebrariam a `A03`, que está
em leito), aritmética do filtergraph de loop, classificação de erros
transitórios, escada de fallback do Veo (com cliente falso), round-trip da
planilha e conformidade completa da saída FFmpeg — inclusive a **ausência de
faixa de áudio**.

---

## Solução de problemas

| Sintoma                                        | Causa provável e correção                                                                      |
| ---------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `Binários ausentes no PATH: ffmpeg`            | Instale o FFmpeg ou aponte `FFMPEG_BIN`/`FFPROBE_BIN`.                                          |
| `storageQuotaExceeded`                         | Service Account sem cota. Defina `DRIVE_ROOT_PARENT_ID` ou `DRIVE_SHARED_DRIVE_ID`.             |
| `GOOGLE_API_KEY não definida`                  | Preencha o `.env`, ou use `--dry-run` para testar sem a API.                                    |
| `429 / RESOURCE_EXHAUSTED` recorrente          | Cota do Veo esgotada. Aumente `VEO_COOLDOWN_BETWEEN_CALLS` e reexecute — o que já foi feito é pulado. |
| `geração bloqueada pelos filtros de segurança` | Suavize a direção clínica do estado em `prompts_data.py`.                                       |
| `acima do teto de 600 KB`                      | Reduza `OUT_FPS`, reduza `OUT_SIZE` ou eleve `MAX_MP4_KB`.                                      |
| `operação não concluiu em 900s`                | Aumente `VEO_POLL_TIMEOUT`.                                                                      |
| Planilha bloqueada pelo Excel                  | O pipeline grava em `*.parcial.xlsx` e avisa. Feche o Excel e reexecute.                        |

---

## Aviso de uso

Os clipes gerados destinam-se a **treinamento e avaliação educacional em
ectoscopia**. São pessoas sintéticas, não pacientes reais, e não constituem
material diagnóstico. A ausência deliberada de sinais patognomônicos é um
requisito pedagógico do material: o objetivo é treinar a leitura do estado
geral, não o reconhecimento de pistas que entreguem a resposta.
