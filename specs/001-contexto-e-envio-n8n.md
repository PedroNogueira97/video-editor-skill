# Spec 001 — `context.json` e envio para o n8n (título, descrição e capa)

| | |
|---|---|
| Status | **implementada em 2026-10-14** (ver "Mudanças na implementação" no fim) |
| Data | 2026-10-13 |
| Afeta | novos: `context.example.json`, `scripts/contexto.py`, `scripts/transcricao_limpa.py`, `scripts/frames.py`, `scripts/enviar_n8n.py`, `tests/teste_n8n.py`; alterados: `.gitignore`, `SKILL.md`, `README.md`, `SPECS.md`, `CLAUDE.md` |
| Não muda | o comportamento de `transcrever.py`, `cortes.py`, `youtube_timeline.py`, `sigilo.py` e `reels_compor.py` |

## 0. Resumo

1. **`context.json`**: um arquivo único com a identidade visual e de copy do canal. Tem um bloco `global` e blocos separados para `youtube` e `reels`. O arquivo é validado ao carregar e fica fora do git. O repositório versiona só o `context.example.json`.
2. **`enviar_n8n.py projeto.json`**: lê a transcrição crua e os cortes do `projeto.json`. Gera um texto limpo (sem ruído e sem falsos começos), remapeado para o tempo do vídeo editado. Escolhe os melhores frames de rosto e envia tudo em multipart para um webhook do n8n. Com `--dry-run`, só grava os arquivos localmente.

A etapa é **nova e opcional**, e roda depois que a edição está aprovada. Nenhum modo atual passa a depender do `context.json`.

---

## 1. Como o código funciona hoje (o que a spec assume)

O pedido cita `montar_timeline.py` e `cortar_clips.py`, mas **esses arquivos não existem**. Os equivalentes são:

| No pedido | No repositório |
|---|---|
| "montar_timeline.py gera a transcrição" | `transcrever.py` chama a ElevenLabs; `cortes.py` gera o `projeto.json` com os cortes; `youtube_timeline.py` gera a timeline e o render |
| "detecção de rosto do cortar_clips.py" | `reels_compor.py → face_track()`, com o YuNet do OpenCV (o MediaPipe não roda nesta CPU; decisão D11) |

### 1.1 Transcrição

- `transcrever.py` salva a resposta **bruta** da API em `<stem>.transcricao.json`, ao lado do vídeo (por exemplo, `Posicionamento.transcricao.json`). Esse arquivo é cache, nunca é reescrito sem `--refazer`, e tem `text`, `words[]` (`text`, `start`, `end`, `type` = `word|spacing`, `logprob`), `language_code`, etc.
- `transcrever.words_of()` já descarta `spacing` e eventos de áudio, mas mantém a pontuação colada na palavra (`"ídolos,"`, `"ti--"`).
- `transcricao.txt` agrupa as frases com pausa de 0,7 s. Isso **não muda**.

### 1.2 Como os cortes são representados

Os cortes ficam no `projeto.json`, em `blocos[]`: `{inicio, fim, tela, removido, motivo}`, no tempo original da câmera.

- O que **fica** no vídeo são os blocos com `removido: false`.
- Silêncio cortado é o espaço **entre** blocos.
- Take removido é um bloco com `removido: true`, mantido no projeto para poder ser restaurado.
- No `reels`, se `gancho` não for nulo, o trecho `{inicio, fim}` toca **antes** de todos os blocos e continua no lugar original. Ou seja, o tempo editado começa com o gancho (`reels_compor.segmentos()`).
- No `youtube`, um bloco com `tela: true` mostra a tela grande e a câmera num canto pequeno. Com `tela: false`, a câmera ocupa a tela inteira.

O mapeamento original → editado já existe em dois lugares: `youtube_timeline.tempo_final()` e `reels_compor.para_timeline()` / `palavras_na_timeline()`. Uma palavra conta como "no vídeo" quando o **ponto médio** dela cai num trecho mantido. A spec reaproveita esse critério para o texto bater com a legenda.

### 1.3 Os falsos começos do fixture **já** são cortados pela timeline

No `Posicionamento.transcricao.json` há exatamente três palavras com `--`, e as três já estão no `takes.csv` aprovado:

| `--` | Tempo | Take na timeline | Coberto? |
|---|---|---|---|
| `ti--` | 107,98 s | T1 105,71–109,33 | sim |
| `f--` | 194,34 s | T2 191,89–197,01 | sim |
| `s--` | 402,62 s | T4 402,27–403,65 | sim |

A timeline também corta retakes **sem** `--` que a regra não pega (C1–C5 e T3, como o primeiro "eu não consigo fingir"). Por isso, o texto limpo **tem que** passar pelos cortes do `projeto.json`. A regra do `--` sozinha não basta.

---

## 2. Parte 1 — `context.json`

### 2.1 Onde fica

- `context.json` fica na **raiz da skill**, ao lado do `.env`. Ele descreve o canal, não um vídeo, e vale para todos os projetos.
- `context.example.json` é versionado. `context.json` entra no `.gitignore`.
- Caminhos relativos resolvem a partir da **raiz da skill**. Também é possível usar caminho absoluto (`/mnt/e/...`).
- O caminho pode ser trocado com `--contexto <arquivo>` ou com a variável de ambiente `EDITAR_VIDEO_CONTEXTO`.
- **Contexto específico de um vídeo** não vai em arquivo. O usuário escreve no prompt ("esse é publi da X, cita o cupom Y"), e o Claude repassa com `--nota "..."` (repetível). As notas vão no payload em `contexto.notas_do_video` (Q2).

### 2.2 Estrutura proposta

É a estrutura mínima do pedido, com as melhorias marcadas com ✚:

```jsonc
{
  "versao": 1,                                   // ✚ para migrar o formato no futuro
  "global": {
    "canal": {"nome": "Pedrinverso", "handle_instagram": "@pedrinverso", "handle_youtube": "@pedrinverso"},
    "idioma": "pt-BR",                           // ✚ vai para o n8n gerar copy no idioma certo
    "tom_de_voz": "direto, informal, provocativo sem ser agressivo",
    "assets": {"fontes_dir": "templates/fontes", "memes_dir": "", "logos_dir": "", "broll_dir": ""},
    "n8n": {
      "webhook_url": "https://n8n.exemplo.com/webhook/editar-video",
      "token_env_var": "N8N_WEBHOOK_TOKEN",
      "timeout_s": 300                           // ✚ resposta síncrona, com geração de imagem
    }
  },
  "youtube": {
    "thumbnail": {
      "largura": 1280, "altura": 720,
      "fonte": "Montserrat.ttf", "peso": 900, "tamanho_px": 140,
      "cor_texto": "#FFFFFF", "cor_destaque": "#FFD400",
      "contorno": {"cor": "#000000", "espessura_px": 8},
      "sombra": {"cor": "#000000AA", "blur_px": 12, "offset_px": [0, 6]},
      "posicao_texto": "esquerda", "max_palavras": 5,
      "remover_fundo": true, "cor_fundo": "#111111",
      "template_path": ""
    },
    "copy": {
      "max_caracteres_titulo": 100,
      "exemplos_titulos": ["..."],
      "descricao_rodape": "...",
      "gerar_capitulos": true,
      "hashtags_fixas": ["#programacao"]
    },
    "frames": {"quantidade_enviada": 6, "candidatos_analisados": 40, "lado_max_px": 1280}   // ✚ lado_max_px
  },
  "reels": {
    "capa": {
      "largura": 1080, "altura": 1920,
      "fonte": "Montserrat.ttf", "peso": 900, "tamanho_px": 120,
      "cor_texto": "#FFFFFF", "cor_destaque": "#FFD400",
      "contorno": {"cor": "#000000", "espessura_px": 8},
      "posicao_texto": "centro", "max_palavras": 5,
      "zona_segura": {"topo_px": 220, "base_px": 420},
      "template_path": ""
    },
    "legenda": {"fonte": "Montserrat.ttf", "cor": "#FFFFFF", "cor_destaque": "#FFD400", "posicao": "centro"},
    "copy": {"max_caracteres_legenda": 2200, "exemplos_legendas": ["..."], "cta": "me segue pra mais", "hashtags_fixas": []},
    "frames": {"quantidade_enviada": 6, "candidatos_analisados": 40, "lado_max_px": 1280}
  }
}
```

**Não incluídos de propósito:**

- O token do webhook: só fica o nome da variável de ambiente.
- Configurações por vídeo: elas continuam no `projeto.json`.

### 2.3 Regras de validação

Feitas à mão em `scripts/contexto.py`, sem dependência nova. Lista **todos** os erros de uma vez, cada um com o caminho completo do campo, e termina com código 1.

```
✖ context.json inválido (3 erros):
  youtube.thumbnail.contorno.espessura_px: esperado inteiro ≥ 0, veio "8px"
  reels.capa.cor_destaque: esperado cor #RRGGBB ou #RRGGBBAA, veio "amarelo"
  global.assets.logos_dir: pasta não existe: /home/pedro/.../logos (resolvido de "logos")
```

| Tipo de campo | Regra |
|---|---|
| Blocos `global`, `youtube`, `reels` e os sub-blocos listados | obrigatórios |
| Campos desconhecidos | **aviso**, não erro, para não travar por erro de digitação de campo opcional; o aviso sugere o nome parecido |
| Cores | `#RRGGBB` ou `#RRGGBBAA` |
| `largura`, `altura`, `tamanho_px`, `*_px` | inteiro > 0 (`espessura_px` e `blur_px` ≥ 0) |
| `offset_px` | lista de 2 inteiros |
| `peso` | inteiro de 100 a 900, ou `"regular"`, `"bold"`, `"black"` (convertidos para 400, 700, 900) |
| `posicao_texto`, `legenda.posicao` | `esquerda`, `direita`, `centro`, `topo`, `base` |
| `max_palavras` | 1 a 10 |
| `max_caracteres_*` | inteiro > 0 (o título do YouTube vai até 100; a legenda do Instagram até 2200) |
| `frames.quantidade_enviada` | ≥ 1 e ≤ `candidatos_analisados` |
| `exemplos_*`, `hashtags_fixas` | lista de strings; hashtags precisam começar com `#` |
| `webhook_url` | `http://` ou `https://`; obrigatório só fora do `--dry-run` |
| `token_env_var` | nome válido de variável; a variável precisa **existir** só fora do `--dry-run` |
| `*_dir` | vazio = não usado; preenchido = a pasta precisa existir |
| `template_path` | vazio = não usado; preenchido = o arquivo precisa existir |
| `fonte` | nome de arquivo procurado em `fontes_dir`, ou caminho; precisa existir |

Se o `context.json` não existir:

```
✖ Não achei o context.json em /home/pedro/.../video-editor-skill/context.json
  Copie o exemplo e preencha:  cp context.example.json context.json
```

O erro só acontece no `enviar_n8n.py`. Os modos `youtube` e `reels` continuam funcionando sem o arquivo (restrição "não quebrar").

### 2.4 Relação com o que já existe no `projeto.json`

Há sobreposição entre o `context.json` e alguns campos do projeto:

| `context.json` | Projeto |
|---|---|
| `reels.legenda` | `projeto.legenda` (cores, posição) |
| `global.canal.handle_*` | `projeto.redes.handle` |

Nesta spec, o `context.json` **só alimenta o n8n**. O render continua lendo o projeto. Ver a dúvida Q3.

---

## 3. Parte 2 — Transformação da transcrição (`scripts/transcricao_limpa.py`)

São funções puras (sem ffmpeg nem rede), testáveis isoladamente. A ordem das etapas importa, e é diferente da numeração do pedido em um ponto. Veja a justificativa em 3.3.

```
JSON cru ──1──▶ palavras ──2──▶ sem retakes "--" ──3──▶ frases (tempo original)
                                                            │
                         projeto.json (segmentos) ──4──▶ frases remapeadas ──5──▶ texto_limpo
```

### 3.1 Descartar ruído

Mantém só `type == "word"` com `start` e `end`, guarda só `text`, `start` e `end` (sem `logprob`) e ordena por `start`. É o mesmo comportamento do `transcrever.words_of()`, que será reutilizado.

### 3.2 Remover falsos começos (`--`)

Um falso começo é uma palavra cujo texto, sem espaços, termina em `--`.

**Normalização para comparar:** minúsculas, sem pontuação (`.,;:!?…"'()`), sem `--`. Os acentos são **mantidos** ("e" ≠ "é").

**Algoritmo**, para cada palavra `k` com `--`, da esquerda para a direita:

1. `seguintes` = as próximas palavras depois de `k`, até 5.
2. Para `n` de 5 até 3, do maior para o menor: procura a sequência `seguintes[:n]` **para trás** a partir de `k-1`, numa janela de **20 s** antes de `k`. Fica a ocorrência mais próxima de `k`.
3. Se achar a ocorrência em `i`, remove as palavras de `i` até `k`, inclusive.
4. Se não achar (ou se houver menos de 3 palavras depois do `--`), volta de `k` até achar uma pausa > 0,5 s entre palavras e remove do início dessa frase até `k`.
   - **Limite de segurança:** se o recuo passar de 6 palavras ou 3 s, remove só a palavra `k` e marca o trecho como `"incerto"`. Sem esse limite, uma fala sem pausas apagaria um parágrafo inteiro.
5. Cada remoção vai para o log com `{inicio, fim, texto_removido, regra: "repeticao|pausa|incerto", palavra_gatilho}`, em `retakes.json` e no terminal.

**Resultado esperado no fixture:**

| Gatilho | Regra | Removido | Fica |
|---|---|---|---|
| `ti--` | repetição: "porque foi o último console" (n=5) | 105,86–108,18 "porque foi o último console em que ti--" | "porque foi o último console em que eu tive acesso a jogos piratas." |
| `f--` | repetição: "e os nossos maiores ídolos" (n=5, `ídolos,` = `ídolos`) | 192,04–194,38 "e os nossos maiores ídolos, o f--" | "…a pirataria e os nossos maiores ídolos disponibilizando tudo de graça," |
| `s--` | pausa: "já me segue…" não aparece antes; pausa de 4,8 s antes de "Me" | 402,42–402,72 "Me s--" | "já me segue e compartilha…" |

**Verificação contra a timeline.** Para cada trecho removido, calcula quanto dele está fora do vídeo (em take removido ou em silêncio cortado):

- **≥ 90 % coberto:** ok, a edição já corta.
- **Não coberto:** aviso `⚠ retake 01:47.98 "…ti--" ainda aparece no vídeo editado`, e o trecho vai para `retakes.json` com `"na_edicao": false`.

**Decisão (Q4, confirmada): a regra do `--` vale só para a transcrição.** Ela nunca altera o `projeto.json` sozinha.

- Alterar a edição é decisão do usuário (D05: takes só com aprovação), e a regra pode errar, principalmente no fallback de pausa.
- Retakes não cobertos passam a ser **sugeridos** no passo 3 do SKILL.md: o Claude roda `enviar_n8n.py --listar-retakes projeto.json`, inclui os achados na tabela de takes e, se o usuário aprovar, aplica pelo `--aplicar-takes` que já existe.
- Assim, a regra também ajuda a montar a lista de takes, que hoje é toda manual.

**Consequência:** se o usuário recusar um retake, o `texto_limpo` não terá aquele trecho, mas o vídeo terá. Para gerar título e descrição, isso é desejável, porque o texto sai sem gagueira. Isso fica registrado no `payload.json` (`retakes_fora_da_edicao`). Ver Q4.

### 3.3 Agrupar em frases

Quebra depois de uma palavra que termina em `.`, `?` ou `!` (inclusive `…`), ou quando a pausa até a próxima palavra é > 1,0 s.

A pausa é medida no **tempo original**. No tempo editado, os silêncios já foram cortados e quase nenhuma pausa passa de 1 s. Por isso, o agrupamento acontece **antes** do remapeamento, como está no pedido. Os retakes saem antes do agrupamento, para que uma frase não comece com um pedaço abandonado.

### 3.4 Remapear para o tempo editado

Usa a **mesma lista de segmentos dos renders**:

- reels: `gancho` (se houver) + blocos ativos;
- youtube: blocos ativos.

A função vai para `transcricao_limpa.segmentos(projeto)`, e o `reels_compor.segmentos()` passa a chamá-la, para não haver duas versões da mesma regra.

- Uma palavra fica se o ponto médio cai num segmento mantido, que é o critério da legenda do Reels.
- `t_editado = início_do_segmento_no_editado + (t_original − segmento.inicio)`.
- Uma frase sem nenhuma palavra restante sai do payload.
- Uma frase parcial fica só com as palavras restantes:
  - `inicio_original` / `inicio_editado` = primeira palavra restante;
  - `fim_editado` = última palavra restante.
- **Frase que atravessa um corte**: as palavras restantes ficam juntas numa frase só, porque no vídeo editado elas são contíguas.
- **Gancho (só reels):** as frases dentro do gancho aparecem duas vezes no vídeo. No payload, aparecem uma vez como segmento com `"gancho": true` no começo, e uma vez no lugar original. Ver Q5.

**Invariantes testados:**

- `inicio_editado` é estritamente crescente.
- `inicio_editado ≤ inicio_original` em todo segmento sem gancho, **quando o projeto não tem gancho**. Com gancho, o restante do vídeo é empurrado pela duração do gancho e o invariante deixa de valer; o teste verifica isso só no fixture, que não tem gancho.

### 3.5 `texto_limpo`

É a concatenação das frases remapeadas, na ordem do vídeo, separadas por espaço. A pontuação original da API é mantida. O segmento do gancho fica de fora, porque é repetição. Não há outra limpeza: maiúsculas, "tá", "pra" e "cê" ficam como foram falados.

---

## 4. Parte 3 — Frames (`scripts/frames.py`)

### 4.1 Onde a câmera é a fonte ativa

| Modo | Janelas válidas (tempo original) |
|---|---|
| youtube | blocos ativos com `tela: false` (câmera em tela cheia) |
| reels | blocos ativos (o gancho é ignorado: é repetição), **menos** os intervalos de `efeitos.imagens` |

Zooms, textos e legenda **não** contam como insert. O frame é extraído do arquivo original da câmera, então nenhuma sobreposição aparece nele.

### 4.2 Candidatos

`candidatos_analisados` tempos distribuídos uniformemente pela **duração editada** das janelas válidas, cada um no centro da sua fatia. Assim, os candidatos seguem o vídeo como ele é assistido, e não o original cheio de takes.

A extração usa um `ffmpeg -ss T -i camera -frames:v 1` por tempo, com seek rápido. A rotação do celular é aplicada automaticamente pelo ffmpeg. No Posicionamento, a câmera é 1080x1920 a 29,97 fps, e 40 seeks custam poucos segundos.

### 4.3 Pontuação

| Critério | Como | Peso |
|---|---|---|
| Rosto detectado e grande | YuNet (o mesmo modelo e cache do `reels_compor.face_track`, extraído para uma função comum); `área do rosto / área do quadro`, normalizada pelo maior valor entre os candidatos | 0,45 |
| Olhos abertos | Haar `haarcascade_eye.xml` (vem no `opencv-python-headless`) na metade de cima da caixa do rosto: 2 olhos = 1, 1 olho = 0,5, nenhum = 0. O cascade detecta mal olho fechado, que é o efeito desejado | 0,25 |
| Nitidez | variância do Laplaciano no recorte do rosto (no quadro todo, se não houver rosto), normalizada pelo maior valor | 0,30 |

- Frame sem rosto fica com score × 0,2. Ele só é enviado se não houver frames suficientes com rosto.
- **Diversidade:** a seleção é gulosa por score, com distância mínima entre frames escolhidos de `duração_editada / (2 × quantidade_enviada)`. Sem isso, os 6 melhores tendem a ser quadros vizinhos quase iguais.
- **Saída:** JPG com qualidade 90, lado maior ≤ `lado_max_px` (padrão 1280), nomeados `frame_01.jpg`, … em ordem de score.

**Alternativas descartadas:** MediaPipe Face Mesh, para medir abertura dos olhos com precisão. Ele aborta nesta CPU (D11).

---

## 5. Parte 4 — Payload e envio (`scripts/enviar_n8n.py`)

### 5.1 Uso

```bash
uv run $S/enviar_n8n.py projeto.json [--frase "texto"]... [--dry-run] [--contexto ARQ] [--listar-retakes]
```

| Flag | Efeito |
|---|---|
| `--frase "..."` | repetível; vai para `frases_destaque`. Avisa se o texto não aparece no `texto_limpo` (compara normalizado) |
| `--nota "..."` | repetível; contexto específico deste vídeo, vindo do prompt; vai em `contexto.notas_do_video` |
| `--dry-run` | gera tudo na pasta de saída e não envia; não exige `webhook_url` nem token |
| `--listar-retakes` | só roda 3.1–3.2, imprime a tabela de retakes e a cobertura pela timeline, e sai (para o passo de takes) |
| `--contexto` | caminho alternativo do `context.json` |

### 5.2 Saída local (sempre gravada, com ou sem `--dry-run`)

```
<pasta do vídeo>/n8n_<stem do projeto>/
  payload.json          # exatamente o JSON enviado no campo "payload"
  frames/frame_01.jpg … frame_06.jpg
  assets/               # cópia dos assets usados na edição que foram enviados (seção 5.4)
  candidatos.json       # todos os candidatos com tempo e notas parciais (para depurar a escolha)
  retakes.json          # log da etapa 3.2
  resposta.json         # resposta do n8n (fora do dry-run)
  capa.png | capa.jpg   # a capa/thumbnail devolvida pelo n8n
  copy.md               # título, descrição/legenda e hashtags, prontos para colar
```

O nome usa o stem do projeto porque uma pasta pode ter `projeto.json` e `projeto_reels.json` (`--saida`). Cada execução sobrescreve a anterior.

O JSON cru da ElevenLabs **não é tocado**. Ele continua em `<stem>.transcricao.json` e o `projeto.json` já aponta para ele (`"transcricao"`). Ver Q8 sobre mandar o caminho no payload.

### 5.3 Payload

O payload é o do pedido, com as adições marcadas com ✚:

```jsonc
{
  "video_id": "posicionamento-3f2a9c1b",        // slug do stem + 8 hex do sha1(caminho da câmera + duração): estável entre execuções
  "modo": "reels",
  "duracao_final_s": 389.4,                      // soma dos segmentos (inclui gancho)
  "texto_limpo": "Eu sou programador e esquerdista. ...",
  "segmentos": [
    {"inicio_editado": 0.0, "fim_editado": 2.08, "inicio_original": 0.58,
     "fim_original": 2.66,                       // ✚
     "texto": "Eu sou programador e esquerdista."}
    // ✚ "gancho": true só nos segmentos do gancho
  ],
  "frases_destaque": [],
  "frames": [{"arquivo": "frame_01.jpg", "tempo_editado": 123.4, "score": 0.87}],
  "contexto": { "global": {...}, "reels": {...},     // só o modo atual; sem o bloco n8n
                "notas_do_video": [] },              // ✚ vindas de --nota
  "gerado_em": "2026-10-13T11:40:00",            // ✚
  "versao_payload": 1,                           // ✚
  "retakes_fora_da_edicao": [],                  // ✚ trechos tirados do texto que ainda estão no vídeo
  "assets_usados": [                             // ✚ o que entrou na edição deste vídeo (seção 5.4)
    {"campo": "asset_01", "arquivo": "linus-torvalds.jpg", "tipo": "imagem", "origem": "efeitos.imagens",
     "tempo_editado": [95.2, 100.1], "credito": "Foto: … CC BY-SA 4.0"},
    {"campo": "asset_02", "arquivo": "Montserrat.ttf", "tipo": "fonte", "origem": "legenda"}
  ],
  "sons_usados": ["impact-bass-1", "pop", "whoosh"]  // ✚ só os nomes, sem arquivo
}
```

- **`contexto`:** é o bloco `global` com o `n8n` inteiro removido. O `webhook_url` e o nome da variável não são úteis para o fluxo e não precisam circular. Junto vai **só** o bloco do modo atual. Os campos de caminho (`*_dir`, `template_path`, `fonte`) vão só com o **nome do arquivo**, sem o caminho local. Os arquivos em si seguem pela regra da seção 5.4.
- **Multipart:** o campo `payload` vai com `Content-Type: application/json`. Cada frame vai num campo **com o mesmo nome do arquivo** (`frame_01`, `frame_02`, …, `image/jpeg`). Campos com nomes distintos evitam ambiguidade no Webhook node do n8n, que transforma cada arquivo numa propriedade binária com o nome do campo.
- **Header:** `Authorization: Bearer <token>`, com o token lido de `os.environ[token_env_var]` ou do `.env` da skill, como já é feito com a `ELEVENLABS_API_KEY`. O token nunca é impresso nem gravado.
- **HTTP:** `requests` (dependência nova, só neste script), sem retry automático, para não disparar o fluxo duas vezes. Como a resposta é síncrona e o fluxo gera uma imagem, o `timeout_s` padrão passa a ser **300**.
  - Resposta ≠ 2xx: mostra o status e os primeiros 500 caracteres do corpo, e termina com código 1. O payload local continua gravado para reenvio.

### 5.4 Assets usados na edição (Q7)

O n8n recebe **o que foi usado na edição deste vídeo**, para saber o que já aparece nele e poder reaproveitar na capa (o logo da publi, a foto citada, a fonte da legenda). Pastas inteiras nunca são enviadas.

O script levanta os assets a partir do `projeto.json` e do `context.json`:

| De onde | O que entra | Vai o arquivo? |
|---|---|---|
| `efeitos.imagens[].arquivo` (reels) | cada imagem inserida (logo, meme, foto, B-roll em imagem), com `tempo_editado` e `credito` | sim |
| fonte da legenda/título do Reels, ou do card das redes no YouTube (hoje `templates/fontes/Montserrat.ttf`) | a fonte realmente usada no render | sim |
| `context.<modo>.thumbnail.fonte` / `capa.fonte` | a fonte da capa, se diferente da anterior | sim |
| `context.<modo>.thumbnail.template_path` / `capa.template_path` | o template da capa, se preenchido | sim |
| `redes.plataformas` (youtube) | os ícones usados no card (`templates/icones/*.svg`) | sim |
| `efeitos.sons[]` | nomes dos sons (`sons_usados`) | não: som não serve para capa |

- **Tipo:** classificado pela pasta do `context.json` onde o arquivo está. Dentro de `memes_dir` vira `meme`, em `logos_dir` vira `logo`, em `broll_dir` vira `broll`; fora delas, `imagem`. É para isso que as pastas servem neste fluxo: dar nome ao que foi usado.
- **Envio:** cada arquivo vai no multipart num campo `asset_01`, `asset_02`…, com o tipo MIME do arquivo, e `assets_usados[].campo` liga o arquivo à entrada do payload.
  - Imagens raster maiores que 1280 px no lado maior são reduzidas, como os frames.
  - SVG, fontes e templates vão como estão.
  - Um mesmo arquivo usado duas vezes vai uma vez só, com todos os intervalos em `tempo_editado`.
- **Arquivo que não existe mais no disco:** aviso, a entrada fica no payload com `"faltando": true` e nenhum arquivo é enviado. O envio não é bloqueado.
- **Cópia local:** os assets enviados também são copiados para `n8n_<projeto>/assets/`, para o `--dry-run` mostrar exatamente o que iria.

### 5.5 Resposta do n8n (síncrona, Q6)

O n8n devolve o resultado na própria resposta do webhook. Contrato proposto: o fluxo termina num nó **Respond to Webhook** com JSON:

```jsonc
{
  "titulo": "…",                       // youtube; no reels pode vir vazio
  "descricao": "…",                    // youtube: descrição completa (com capítulos e rodapé)
  "legenda": "…",                      // reels: legenda do post (com CTA)
  "hashtags": ["#…"],
  "capitulos": [{"tempo": "00:00", "titulo": "…"}],   // opcional, youtube
  "capa": {"base64": "…", "formato": "png"}           // ou {"url": "https://…"}
}
```

- Campos ausentes são aceitos. O script só falha se a resposta não for JSON.
- **Capa:** se vier `base64`, ela é decodificada. Se vier `url`, é baixada com o mesmo timeout. Em ambos os casos, é salva em `capa.<formato>` e as dimensões são conferidas contra o `context.json`. Tamanho diferente gera aviso, não erro.
- Também é gravado um **`copy.md`** com título, descrição/legenda, hashtags e capítulos, prontos para colar.
- **Validação leve:** avisa se o título passa de `max_caracteres_titulo` ou se a legenda passa de `max_caracteres_legenda`.
- No fim, o terminal mostra o título e o caminho Windows (`wslpath -w`) da capa e do `copy.md`. O Claude mostra isso ao usuário e pergunta se quer gerar de novo, com `--frase`/`--nota` diferentes.

---

## 6. Testes (`tests/teste_n8n.py`, executado com `uv run --script`)

**Nenhuma transcrição real vai para o repositório (Q1).** Os testes têm duas camadas:

1. **Sintética (sempre roda).** O próprio `teste_n8n.py` monta, em código, uma transcrição falsa no formato da ElevenLabs, com `spacing` e `logprob`. As frases são inventadas e reproduzem os três padrões: repetição de 5 palavras depois do `--`, pontuação colada (`ídolos,`) e falso começo sem repetição, resolvido pela pausa. O teste também monta um `projeto.json` sintético com blocos, takes e, num caso, gancho.
2. **Real (opcional, local).** Com `TRANSCRICAO_REAL=/mnt/e/reels-posicionamento/Posicionamento.transcricao.json` e `PROJETO_REAL=/mnt/e/reels-posicionamento/projeto.json`, roda também os casos abaixo do Posicionamento. Sem as variáveis, o teste é pulado com aviso.

Por segurança, `*.transcricao.json`, `transcricao.txt`, `projeto.json*`, `context.json` e `n8n_*/` entram no `.gitignore`.

Casos (os números são do Posicionamento; a camada sintética verifica os mesmos comportamentos com tempos próprios):

| Caso | Verificação |
|---|---|
| Ruído | nenhuma entrada `spacing` e nenhuma chave `logprob` em lugar nenhum do payload |
| `ti--` | log com regra `repeticao`, 105,86–108,18; `texto_limpo` contém "porque foi o último console em que eu tive acesso a jogos piratas" uma vez e não contém "ti--" |
| `f--` | regra `repeticao`, 192,04–194,38; contém "e os nossos maiores ídolos disponibilizando tudo de graça" e não contém "o f--" |
| `s--` | regra `pausa`, 402,42–402,72; contém "já me segue e compartilha" e não contém "Me s--" |
| Cobertura | os três retakes têm `na_edicao: true` e `retakes_fora_da_edicao` fica vazio |
| Takes sem `--` | "Mas tem uma coisa que eu acho muito curiosa" (C1) **não** aparece no `texto_limpo`, porque é cortado pela timeline |
| Invariantes | `inicio_editado` estritamente crescente; `inicio_editado ≤ inicio_original` em todos os segmentos |
| Gancho | projeto sintético com gancho: o segmento `gancho: true` vem primeiro e o texto dele não se repete no `texto_limpo` |
| Fallback limitado | entrada sintética sem pausa: o recuo para em 6 palavras / 3 s e o trecho fica marcado `incerto` |
| Contexto | arquivo ausente gera a mensagem de "copie o exemplo"; campos errados geram mensagens com o caminho exato; o `context.example.json` passa na validação |
| Dry-run | gera `payload.json` e `frames/`, não faz chamada HTTP (servidor local falso que falha se receber requisição) |
| Envio | servidor HTTP local recebe multipart com `payload` + `frame_01..N`, header `Authorization: Bearer x` |
| Assets usados | projeto sintético com 2 imagens em `efeitos.imagens` (uma em `logos_dir`, uma usada duas vezes) + 1 ausente: multipart tem `asset_01..N` sem duplicata, tipos `logo`/`imagem`, a ausente marcada `faltando`; sons só em `sons_usados` |
| Resposta | servidor falso devolve o JSON do 5.5 com `capa.base64` (e, noutro caso, `capa.url`): o teste confere `capa.png`, `copy.md` e o aviso de título longo |
| Frames | no `teste_sintetico.sh`: os frames escolhidos caem só em blocos `tela: false` (youtube) e o lado maior é ≤ 1280 |

Os testes atuais (`teste_sintetico.sh`, `teste_enquadramento.py`) continuam passando sem mudanças. Esse é o critério da restrição "não quebrar".

---

## 7. Documentação

- **README.md:** nova seção "Título, descrição e capa (n8n)", com `cp context.example.json context.json`, a variável `N8N_WEBHOOK_TOKEN` (ambiente ou `.env`), `--dry-run` e a pasta `n8n_<projeto>/`.
- **SKILL.md:**
  - passo 3 passa a usar `--listar-retakes` para sugerir takes;
  - novo passo final nos dois modos: "depois do render aprovado, perguntar se quer gerar título/descrição/capa; perguntar se há frases de destaque; rodar com `--dry-run` primeiro se for a primeira vez".
- **SPECS.md:** seção do `context.json` e do payload. **CLAUDE.md:** `context.json` nunca vai para o git.
- **`.claude/archive/decisoes.md`:** registrar as decisões D16–D18 (retake só na transcrição; YuNet + Haar para frames; multipart com campos nomeados).

## 8. Fora do escopo

- O fluxo do n8n em si: geração de título, descrição e thumbnail.
- Usar o `context.json` no render ou na legenda (Q3).
- Legendas a partir dos timestamps por palavra. O JSON cru continua preservado para isso.
- Aplicar automaticamente na timeline o que o n8n devolver.

---

## 9. Decisões e dúvidas

### Decididas

- **Q1 — Transcrições nunca vão para o repositório.** Os testes usam uma transcrição sintética, montada no próprio teste. O Posicionamento serve só de teste local opcional, via variável de ambiente (seção 6). `*.transcricao.json`, `transcricao.txt`, `projeto.json*`, `context.json` e `n8n_*/` entram no `.gitignore`.
- **Q2 — `context.json` na raiz da skill.** Contexto específico de um vídeo vem do prompt e o Claude repassa com `--nota` (seção 2.1).
- **Q4 — A regra do `--` vale só para a transcrição.** Retake não coberto pela timeline vira sugestão de take, aplicada só com aprovação.
- **Q6 — Resposta síncrona.** O n8n devolve título, descrição/legenda e capa na resposta. O script salva `capa.*`, `copy.md` e `resposta.json` (seção 5.5).
- **Q7 — Vai para o n8n o que foi usado na edição.** São enviados, como arquivos, as imagens inseridas, as fontes, os ícones do card e o template da capa, e só os nomes dos sons. As pastas do `context.json` servem para classificar cada asset (meme, logo, B-roll) e nunca são enviadas inteiras (seção 5.4).

### Assumidas (sem resposta; mude se discordar)

- **Q3:** o `context.json` não alimenta o `projeto.json` nesta spec. Unificar `reels.legenda` e os handles com o projeto fica para uma spec 002.
- **Q5:** o gancho entra nos segmentos marcado `gancho: true` e fica fora do `texto_limpo`.
- **Q8:** o payload não leva o caminho do JSON cru. Ele continua ao lado do vídeo e apontado no `projeto.json`.
- **Q9:** janela de busca de 20 s, recuo máximo de 6 palavras / 3 s e cobertura de 90 %.

### Abertas

Nenhuma.

## 10. Plano de implementação (depois da aprovação)

1. `context.example.json`, `contexto.py` + testes de validação; `.gitignore`.
2. `transcricao_limpa.py` (3.1–3.5) + testes sintéticos (e o teste local opcional com o Posicionamento); `reels_compor.segmentos()` passa a usar a função comum (conferido pelo `teste_sintetico.sh`).
3. Extração da função de rosto do YuNet para um ponto comum; `frames.py` + teste.
4. `enviar_n8n.py` (`--dry-run`, `--listar-retakes`, `--frase`, `--nota`, envio, leitura da resposta com capa e `copy.md`) + teste com servidor local.
5. README, SKILL.md, SPECS.md, CLAUDE.md, decisões.
6. Rodar `teste_sintetico.sh`, `teste_enquadramento.py` e `teste_n8n.py`; um `--dry-run` real no Posicionamento para você conferir os frames e o `texto_limpo`.

## 11. Mudanças na implementação (2026-10-14)

- **Estrutura do `context.json`:** a seção 2.2 foi substituída pela estrutura que o usuário definiu, e o `context.example.json` é a referência.
  - **Saíram:** `versao`, `idioma`, `timeout_s` (o padrão interno é 300 s), `template_path`, `remover_fundo`, `cor_fundo`, `tamanho_px` e `lado_max_px` (fixo em 1280).
  - **Entraram:** `identidade`, `tipografia_base`, `assets.logos`, `referencia_thumb`, `composicao`, `instrucoes_extra`, `exemplos_texto_thumb`, `tarjas`, `max_linhas`, `zona_segura.laterais_px`/`grid_perfil_3x4` e o bloco `reels.legenda` ampliado.
- **`fonte` da thumbnail e da capa** é uma descrição livre do estilo. Já `tipografia_base.fonte` e `reels.legenda.fonte` são nomes procurados em `fontes_dir`; se o arquivo não for achado, há aviso.
- **Caminho de asset ausente** gera aviso, não erro, a pedido do usuário. Isso inclui `referencia_thumb`: sem ela, o envio segue sem o campo.
- **`template_path` virou `referencia_thumb`.** A imagem vai no campo `referencia_thumb` do multipart, e o nome do arquivo vai no campo `referencia_thumb` do payload.
- **Logos citados na fala:** os logos de `global.assets.logos` cujo nome aparece no texto limpo também são anexados (`origem: "mencionado_na_fala:<nome>"`). É o que a `instrucoes_extra` da thumbnail pede ("use o logo sem redesenhar").
- **OpenCV:** o `enviar_n8n.py` fixa `opencv-python-headless<5`, porque o `CascadeClassifier` saiu do OpenCV 5.0.
- **`reels_compor.segmentos()` não foi refatorado.** O `transcricao_limpa.segmentos()` replica a mesma regra, para não arriscar o render atual.
- **Q3 mudou:** o usuário quer que o render passe a seguir o contexto (fonte e cores da legenda, cor de marca). Isso fica para a spec 002 (decisão D19).
