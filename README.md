# editar-video

Skill do [Claude Code](https://claude.com/claude-code) que faz o **primeiro corte automático** de vídeos gravados em casa. Ela transcreve, tira silêncios e takes repetidos, monta a edição e renderiza o MP4 final. No fim, manda o vídeo editado para um fluxo do n8n, que gera título, descrição ou legenda e a capa. Você aprova tudo no navegador e não precisa abrir um editor de vídeo.

São dois modos:

| Modo | Para quê | Entrada | Revisão | Saída |
|---|---|---|---|---|
| `youtube` | Vídeo longo (~40 min), câmera + gravação de tela | câmera (com áudio) + tela (sem áudio) | `timeline.html` no navegador | `video_final.mp4` (ffmpeg/x264) |
| `reels` | Vídeo curto vertical (até ~90 s) | um vídeo | Studio do [HyperFrames](https://www.npmjs.com/package/hyperframes) | `reels_final.mp4` 1080x1920, 30 fps |

## Como funciona

```
                 ┌──────────────────┐
 camera.mp4 ───▶ │ transcrever.py   │──▶ <video>.transcricao.json (cache) + transcricao.txt
                 └──────────────────┘
                          │   o Claude lê a transcrição, acha os takes repetidos
                          │   (inclusive os falsos começos "--" da API)
                          ▼   e grava takes.csv depois que você aprova
                 ┌──────────────────┐
 tela.mov ─────▶ │ cortes.py        │──▶ projeto.json   (fonte única da verdade)
                 └──────────────────┘
                     │                         │
          youtube    ▼                         ▼    reels
 ┌─────────────────────────┐        ┌──────────────────────────┐
 │ sigilo.py  (opcional)   │        │ reels_compor.py          │
 │ youtube_timeline.py     │        │  --preview / --renderizar│
 │  --servir / --renderizar│        └──────────────────────────┘
 └─────────────────────────┘                   │
              │                                ▼
              ▼                         reels_final.mp4
       video_final.mp4                         │
              └──────────────┬─────────────────┘
                             ▼   depois do render aprovado
                 ┌──────────────────────┐
 context.json ─▶ │ enviar_n8n.py        │──▶ n8n_<projeto>/: payload, frames, assets
                 └──────────────────────┘
                             │  multipart → webhook do n8n (resposta síncrona)
                             ▼
                 título · descrição/legenda · hashtags · capa  (copy.md + capa.*)
```

Toda a edição fica no `projeto.json`: blocos de fala, takes removidos, trechos de tela, offset de sincronia, borrões, gancho, legenda e efeitos. A timeline, a composição do Reels e os dois renders leem desse arquivo. Todos os tempos estão no **tempo original da câmera**.

O `context.json` guarda a identidade do canal (tom de voz, cores, tipografia, regras de thumbnail, capa e copy) e só é usado pelo `enviar_n8n.py`.

## Requisitos

- **Linux ou WSL** (foi feita e testada no WSL Ubuntu dentro do Windows 10). Tudo roda **na CPU**, sem GPU.
- [`uv`](https://docs.astral.sh/uv/): cada script declara as próprias dependências (PEP 723), então o `uv run` instala tudo sozinho.
- `ffmpeg` / `ffprobe` no `PATH`.
- Uma chave da **ElevenLabs** (transcrição Scribe `scribe_v2`, com tempo por palavra) em `ELEVENLABS_API_KEY`, no ambiente ou num `.env` na raiz da skill (ele está no `.gitignore`). Sem a chave, use `--sem-api` no `cortes.py` (corta pelo `silencedetect`, sem transcrição).
- Só para o modo `reels`: **Node 22** e o HyperFrames (`npx hyperframes doctor` precisa mostrar Node, FFmpeg e Chrome ok; se faltar o Chrome, rode `npx hyperframes browser ensure`, que precisa do `unzip`).
- Chrome para revisar (no WSL, o Chrome do Windows abre o `localhost`).

### Dependências Python (instaladas pelo `uv`)

| Script | Dependências |
|---|---|
| `transcrever.py` | `elevenlabs` |
| `cortes.py` | `numpy`, `elevenlabs` |
| `youtube_timeline.py` | `numpy`, `elevenlabs`, `opentimelineio` |
| `sigilo.py` | `numpy`, `elevenlabs`, `rapidocr`, `onnxruntime`, `opencv-python-headless` |
| `reels_compor.py` | `numpy`, `elevenlabs`, `opencv-python-headless` |
| `enviar_n8n.py` | `numpy`, `elevenlabs`, `opencv-python-headless<5` (o detector de olhos saiu do OpenCV 5), `requests` |

Python ≥ 3.9.

## Instalação

Clone o repositório e crie um link para ele na pasta de skills do Claude Code:

```bash
git clone git@github.com:PedroNogueira97/video-editor-skill.git
ln -s "$PWD/video-editor-skill" ~/.claude/skills/editar-video
echo 'ELEVENLABS_API_KEY=...' > ~/.claude/skills/editar-video/.env

# para a etapa do n8n (título, descrição e capa)
cd ~/.claude/skills/editar-video
cp context.example.json context.json     # preencha com a identidade do seu canal
echo 'N8N_WEBHOOK_TOKEN=...' >> .env
uv run scripts/contexto.py               # valida o context.json
```

Os assets do canal (referências de thumbnail e capa, logos, memes, B-roll e fontes) ficam em `assets/`, que está fora do git.

O nome da skill vem do `name:` do [SKILL.md](SKILL.md) (`editar-video`).

## Uso pela skill

No Claude Code:

```
/editar-video youtube "E:\Gravações\aula-07"
/editar-video reels "E:\Gravações\reels\video.mp4"
```

Se você não disser o modo, o Claude pergunta. Caminhos do Windows são convertidos com `wslpath`. O Claude conduz o fluxo e para nos pontos de aprovação:

1. **Transcreve** a câmera, que é o arquivo com áudio. A API é paga só uma vez: depois o cache `<video>.transcricao.json` é reaproveitado.
2. **Lista os takes repetidos** numa tabela com timecode, e mantém sempre o último take. Isso inclui "vou falar de novo", frases recomeçadas, comentários de bastidor e os falsos começos que a API marca com `--` ("ti--"). Ele **espera seu ok** antes de gravar o `takes.csv`.
3. **Gera o `projeto.json`** com os silêncios e os takes cortados.
4. **Revisão e render**, que dependem do modo (veja abaixo).
5. **Título, descrição/legenda e capa.** Depois do render aprovado, o Claude pergunta se você quer gerar a copy e a capa, e se há frases de destaque. Também repassa o contexto específico que você der no pedido ("é publi da X"). Ele roda o `enviar_n8n.py`: um `--dry-run` na primeira vez, para você conferir, e depois o envio. Por fim, mostra o título ou a legenda e o caminho da capa. Detalhes em [Título, descrição e capa (n8n)](#título-descrição-e-capa-n8n).

### Modo `youtube`

- Abre a **timeline** em `http://localhost:8765/timeline.html`, com proxies 480p. As cores são: fala (verde), fala + tela (azul), take removido (vermelho) e silêncio cortado (cinza). O preview já mostra o layout final.
  - Atalhos: `T` liga/desliga a tela · `Del`/`R` remove/restaura · `S` divide no cursor · `Ctrl+Z` desfaz · `Espaço`, `J/K/L` e setas para navegar · clique na transcrição para pular até o trecho.
  - Arraste as bordas do bloco selecionado para ajustar. O **Salvar** grava direto no `projeto.json` e deixa um backup `.bak`.
- **Sincronia fala ↔ tela**: o offset inicial vem da data dos arquivos (e desconta que o celular grava a data do *fim* da gravação). O Claude confere por eventos visíveis com folhas de contato e ajusta o `"offset"`.
- **Dados sigilosos** (`sigilo.py`): OCR na CPU só na tela que entra no vídeo. Procura e-mail, telefone, CPF/CNPJ e tokens e grava `"borroes"`. Você desmarca na timeline o que pode aparecer.
- **Card das redes sociais**: um lower third com ícones (instagram, tiktok, youtube, linkedin, x, github) que entra nos momentos definidos em `"sobreposicoes"`.
- **Render**: tela grande à esquerda e câmera no canto inferior direito nos blocos com tela; câmera em tela cheia no resto. Renderiza trecho a trecho em `render_cache/`, então se parar é só rodar de novo.
- Opcional: `--otio` exporta para o Kdenlive 25.04+.

### Modo `reels`

- **Gancho**: o Claude sugere abrir com a frase mais forte (2 a 4 s) e pergunta antes de aplicar.
- **Composição**: gera `reels_<video>/` (um projeto HyperFrames) com `base.mp4` em 1080x1920. Vídeo horizontal é reenquadrado **seguindo o rosto** com o detector YuNet do OpenCV. A legenda é karaokê, com a palavra falada destacada na área segura do Reels. O preview abre em `http://localhost:3002`.
- **Ajustes em texto**: você pede ("corta a parte do 0:12", "legenda maior", "tira o gancho"), o Claude edita o `projeto.json`, roda de novo e o preview recarrega.
- **Edição dinâmica** (zooms, efeitos sonoros, imagens, textos e manchete fixa), com tempos batendo nas palavras fortes:

  ```json
  "efeitos": {
    "zooms":   [{"inicio": 7.54, "fim": 9.0, "escala": 1.35, "estilo": "impacto"}],
    "sons":    [{"t": 7.54, "som": "impact-bass-1", "volume": 0.45}],
    "imagens": [{"inicio": 11.4, "fim": 16, "arquivo": "/mnt/e/.../logo.svg", "largura": 720, "y": 0.11}],
    "textos":  [{"inicio": 52.2, "fim": 53.3, "texto": "1. se cadastra", "y": 0.13, "fundo": "#FF007E"}]
  },
  "titulo": {"texto": "Programador pode ser de *esquerda?*", "y": 0.11}
  ```

  Os estilos de zoom são `punch` (salto seco), `suave` (aproxima ao longo do trecho) e `impacto` (salto + tremida). Os sons ficam em [templates/sfx/](templates/sfx/), com metadados no `manifest.json`.
- **Render**: H.264 + AAC, 30 fps, 1080x1920. Leva ~1,7 min por segundo de vídeo numa CPU antiga (i3-2100). Renderize a partir do disco do WSL, não de `/mnt/*`.

## Título, descrição e capa (n8n)

Depois da edição aprovada, o `enviar_n8n.py` manda o vídeo editado para um webhook do n8n, que devolve na mesma resposta o título, a descrição (YouTube) ou a legenda (Reels), as hashtags e a capa/thumbnail.

O envio leva:
- o texto limpo do vídeo editado: sem ruído da API, sem falsos começos marcados com `--` e sem o que foi cortado na timeline;
- as frases com tempo editado e tempo original;
- os melhores frames de rosto da câmera;
- a imagem de referência da capa;
- os assets usados na edição;
- o contexto do canal.

### 1. Criar o `context.json`

```bash
cp context.example.json context.json      # na raiz da skill
uv run scripts/contexto.py                # valida e mostra erros e avisos
```

O `context.json` e a pasta `assets/` ficam fora do git (`.gitignore`). Só o `context.example.json` é versionado.

Se o arquivo não existir, o `enviar_n8n.py` para e manda copiar o exemplo. Os modos `youtube` e `reels` funcionam sem ele.

A validação lista todos os problemas de uma vez, com o caminho do campo:
- **Erro:** campo obrigatório ausente, tipo errado, cor fora de `#RRGGBB`/`#RRGGBBAA`, posição ou peso inválidos.
- **Aviso:** caminho de asset (pasta, logo, referência da thumb, fonte) que não existe no disco. A skill segue sem ele.

Caminhos relativos resolvem a partir da raiz da skill.

### 2. Token do webhook

O token **não** fica no `context.json`. Lá vai só o nome da variável (`global.n8n.token_env_var`, padrão `N8N_WEBHOOK_TOKEN`). Defina o valor no ambiente ou no `.env` da skill:

```bash
echo 'N8N_WEBHOOK_TOKEN=...' >> ~/.claude/skills/editar-video/.env
```

Ele é enviado como `Authorization: Bearer <token>` e nunca é impresso nem gravado.

### 3. Rodar

```bash
uv run $S/enviar_n8n.py projeto.json --dry-run          # gera tudo localmente, sem enviar
uv run $S/enviar_n8n.py projeto.json                    # envia e salva a resposta
uv run $S/enviar_n8n.py projeto.json --frase "frase forte do vídeo" --nota "é publi da marca X"
uv run $S/enviar_n8n.py projeto.json --listar-retakes   # só lista os falsos começos "--"
```

| Flag | Efeito |
|---|---|
| `--dry-run` | grava payload, frames e assets em `n8n_<projeto>/` e não envia; não exige URL nem token |
| `--frase "..."` | frase de destaque (repetível); sem ela, o n8n decide |
| `--nota "..."` | contexto específico deste vídeo, vindo do seu pedido (repetível) |
| `--listar-retakes` | lista os falsos começos `--` e diz se a timeline já os corta |
| `--contexto ARQ` | usa outro `context.json` |

A saída fica ao lado do vídeo, em `n8n_<projeto>/`:

| Arquivo | Conteúdo |
|---|---|
| `payload.json` | o JSON enviado no campo `payload`; sempre gravado, inclusive no `--dry-run` |
| `frames/` | os frames escolhidos |
| `assets/` | os assets usados na edição |
| referência da capa | cópia da imagem de `referencia_thumb` |
| `retakes.json` | o log dos falsos começos |
| `candidatos.json` | as notas de todos os frames analisados |
| `resposta.json`, `capa.*`, `copy.md` | a resposta do n8n, quando há envio |

O JSON cru da ElevenLabs (`<video>.transcricao.json`) nunca é alterado.

**Multipart enviado:**
- `payload` (JSON);
- `frame_01` … `frame_NN` (JPG, lado maior ≤ 1280 px);
- `referencia_thumb` (a imagem de referência do modo atual; o nome do arquivo vai também no campo `referencia_thumb` do payload);
- `asset_01` … (os assets usados na edição, ligados ao payload por `assets_usados[].campo`).

### Blocos do `context.json`

O arquivo tem três blocos: `global` vale para os dois modos, e `youtube` e `reels` ficam separados. O payload leva o `global` (sem o bloco `n8n`) e **só** o bloco do modo do vídeo. Os caminhos vão reduzidos ao nome do arquivo.

**`global`**

| Campo | Para quê |
|---|---|
| `canal` | `nome`, `handle_instagram`, `handle_youtube` (pode ficar vazio) |
| `tom_de_voz` | como você fala e o que evitar; guia título, descrição e legenda |
| `identidade` | `cor_primaria`, `cor_secundaria`, `cor_marca` (hex) |
| `tipografia_base` | `fonte` (nome; o arquivo é procurado em `fontes_dir`), `altura_linha`, `contorno` {`cor`, `espessura_px`} |
| `assets` | `fontes_dir`, `memes_dir`, `logos_dir`, `broll_dir`: as pastas classificam cada asset usado (meme, logo, B-roll) e nunca são enviadas inteiras. `logos`: mapa nome → arquivo; quando a fala cita o nome ("claude", "chat gpt"), o logo é anexado |
| `n8n` | `webhook_url` e `token_env_var` (o **nome** da variável do token). Não vai no payload |

**`youtube`**

| Campo | Para quê |
|---|---|
| `thumbnail` | `largura`/`altura`; `referencia_thumb` (imagem de referência, anexada no envio); `fonte` (descrição livre do estilo) e `peso`; `cor_texto`, `cor_destaque`; `contorno`, `sombra` {`cor`, `blur_px`, `offset_px`}; `posicao_texto` (`topo`, `base`, `centro`, `esquerda`, `direita`); `max_palavras`; `composicao`; `instrucoes_extra` (instruções livres de composição) |
| `copy` | `max_caracteres_titulo` (até 100), `exemplos_titulos`, `exemplos_texto_thumb`, `descricao_rodape`, `gerar_capitulos`, `hashtags_fixas` |
| `frames` | `quantidade_enviada` e `candidatos_analisados` (frames só de trechos com a câmera em tela cheia) |

**`reels`**

| Campo | Para quê |
|---|---|
| `capa` | `largura`/`altura`; `referencia_thumb`; `fonte`, `peso`; `estilo_texto` e `tarjas` [{`cor_tarja`, `cor_texto`}]; `posicao_texto`; `max_palavras`, `max_linhas`; `zona_segura` {`topo_px`, `base_px`, `laterais_px`, `grid_perfil_3x4`}; `instrucoes_extra` |
| `legenda` | `fonte`, `altura_linha`, `cor`, `contorno`, `fundo` (`"nenhum"` ou hex), `posicao`. Hoje só informa o n8n: o render do Reels ainda usa a legenda do `projeto.json` |
| `copy` | `max_caracteres_legenda` (até 2200), `exemplos_legendas`, `exemplos_texto_thumb`, `cta`, `hashtags_fixas` |
| `frames` | como no youtube; no Reels, ficam de fora os trechos com imagem inserida |

## Uso direto dos scripts

Os scripts também funcionam sem o Claude:

```bash
S=~/.claude/skills/editar-video/scripts

# base comum
uv run $S/transcrever.py camera.mp4
uv run $S/cortes.py youtube camera.mp4 tela.mov --takes takes.csv
uv run $S/cortes.py reels video.mp4 --takes takes.csv

# youtube
uv run $S/youtube_timeline.py projeto.json --servir
uv run $S/sigilo.py projeto.json
uv run $S/youtube_timeline.py projeto.json --renderizar

# reels
uv run $S/reels_compor.py projeto.json --preview
uv run $S/reels_compor.py projeto.json --renderizar
```

Formato do `takes.csv` (tempo original da câmera, em segundos):

```csv
inicio,fim,motivo
190.2,198.95,"vou falar de novo"
```

### Flags

**`cortes.py`** `modo camera [tela]`

| Flag | Padrão | Efeito |
|---|---|---|
| `--takes CSV` | | aplica os takes aprovados |
| `--aplicar-takes CSV --projeto P` | | marca takes novos num projeto já editado, sem perder as edições |
| `--saida` | `projeto.json` | nome do projeto (útil quando há vídeos dos dois modos na mesma pasta) |
| `--sobrescrever` | | refaz do zero e guarda o antigo em `.bak` |
| `--silencio-min` | 0.7 youtube / 0.35 reels | menor pausa (s) que é cortada |
| `--respiro` | 0.15 | margem (s) antes e depois da fala |
| `--sem-api` | | usa o `silencedetect` do ffmpeg em vez da transcrição |
| `--ruido` | -35 | dB abaixo do qual é silêncio (`--sem-api` e palavras longas) |
| `--palavra-max` | 2.0 | palavra mais longa que N s é conferida com o `silencedetect` |
| `--clipe-min` | 0.25 | sobras menores que N s na borda de um take são removidas |
| `--refazer-transcricao` | | ignora o cache e chama a API de novo |
| `--offset` | data dos arquivos | segundos que a tela começou depois da câmera |
| `--tela-csv` | | usa um CSV de trechos de tela em vez de detectar |
| `--sensibilidade` | 0.8 | sensibilidade da detecção de movimento na tela |
| `--juntar` | 10 | junta trechos de tela separados por menos de N s |
| `--tela-min` | 4 | menor trecho de tela (s) |
| `--folga-tela` | 1.5 | folga (s) em volta dos trechos de tela |

**`youtube_timeline.py`** `projeto.json`: `--servir` (`--porta` 8765, `--host` 127.0.0.1), `--renderizar [MP4]`, `--crf` (20), `--preset` (medium), `--otio [ARQ]`.

**`sigilo.py`** `projeto.json`: `--intervalo-max` (15 s), `--limiar` (2.0), `--margem` (8 px), `--so-listar`.

**`reels_compor.py`** `projeto.json`: `--preview` (`--porta` 3002), `--renderizar [MP4]`, `--pasta`, `--workers` (`auto`; cada processo do Chrome usa ~256 MB).

**`transcrever.py`** `camera`: `--txt caminho`, `--refazer`.

**`enviar_n8n.py`** `projeto.json`: `--dry-run`, `--frase` (repetível), `--nota` (repetível), `--listar-retakes`, `--contexto ARQ`.

## Estrutura do repositório

```
SKILL.md                  instruções que o Claude segue (fluxo, aprovações, problemas comuns)
SPECS.md                  especificação técnica (arquitetura, schema do projeto.json, scripts)
CLAUDE.md                 guia para desenvolver o repositório com o Claude Code
.claude/archive/          histórico de decisões técnicas
.claude/skills/           symlink da skill, para usá-la com o Claude Code aberto nesta pasta
scripts/
  transcrever.py          ElevenLabs Scribe → JSON com tempo por palavra + transcricao.txt
  cortes.py               transcrição → silêncios, takes, sincronia da tela → projeto.json
  youtube_timeline.py     proxies, timeline.html, servidor local, render ffmpeg, export OTIO
  sigilo.py               OCR (RapidOCR) da tela → "borroes" no projeto.json
  reels_compor.py         reenquadramento vertical, legenda karaokê, efeitos → HyperFrames
  enviar_n8n.py           payload + frames + assets → webhook do n8n (título, descrição, capa)
  contexto.py             carrega e valida o context.json
  transcricao_limpa.py    transcrição crua → texto limpo no tempo editado (sem retakes "--")
  frames.py               escolhe os melhores frames de rosto da câmera
context.example.json      modelo do context.json (o real fica fora do git)
specs/                    specs de funcionalidades (decisões e dúvidas antes de implementar)
templates/
  timeline.html           interface da timeline do modo youtube
  fontes/                 Montserrat (OFL)
  icones/                 ícones das redes sociais (CC0)
  sfx/                    efeitos sonoros (Pixabay Content License) + manifest.json
  vendor/gsap.min.js      animações da composição do Reels
tests/
  teste_sintetico.sh      teste de ponta a ponta dos dois modos
  teste_enquadramento.py  teste do reenquadramento pelo rosto
  teste_n8n.py            context.json, transcrição limpa, --dry-run e envio (dados sintéticos)
  navegador.mjs           checa a timeline num Chrome headless (puppeteer-core)
  mock/elevenlabs/        SDK falso da ElevenLabs para os testes
```

## Testes

```bash
bash tests/teste_sintetico.sh [pasta_de_trabalho]
```

O teste gera vídeos sintéticos com ffmpeg, troca a ElevenLabs por um mock (sem custo) e valida:

- o `projeto.json` dos dois modos;
- a `timeline.html` num Chrome headless: sem erros no console e com o Salvar funcionando por download e pelo servidor;
- o render do YouTube (duração e layout) e o export OTIO;
- a composição e o render do Reels (duração, H.264/AAC, 1080x1920, 30 fps).

Para o fluxo do n8n:

```bash
uv run --script tests/teste_n8n.py
```

Ele usa só dados sintéticos e um n8n falso local. Nenhuma transcrição real vai para o repositório. Para conferir também contra um vídeo seu, defina `TRANSCRICAO_REAL` e `PROJETO_REAL` com os caminhos locais.

A pasta padrão do `teste_sintetico.sh` fica em `/mnt/c/Users/Public/teste editar video`, que tem espaço no nome de propósito para testar caminhos do Windows. Na primeira vez, o teste instala o `puppeteer-core` em `tests/`.

## Problemas comuns

A tabela completa está no [SKILL.md](SKILL.md#problemas-comuns). Os mais frequentes:

| Sintoma | Ajuste |
|---|---|
| Cortes secos, comendo palavras | `--respiro 0.25` |
| Ritmo picotado / sobram pausas | `--silencio-min` maior / menor |
| Tela adiantada ou atrasada | ajuste `"offset"` no `projeto.json` |
| `localhost` não abre no Chrome do Windows | `--host 0.0.0.0` |
| Render do YouTube parou | rode de novo; o `render_cache/` reaproveita o que já foi feito |
| Reels: rosto cortado ou quadro balançando | `enquadramento.seguir_rosto: false` |
| Render do Reels trava | copie `reels_<video>/` para o disco do WSL e use `--pasta` |

## Licenças dos recursos

- Fonte Montserrat: SIL Open Font License ([templates/fontes/OFL.txt](templates/fontes/OFL.txt))
- Ícones: CC0 1.0 ([templates/icones/LICENSE.md](templates/icones/LICENSE.md))
- Efeitos sonoros: Pixabay Content License ([templates/sfx/CREDITS.md](templates/sfx/CREDITS.md))
- GSAP: licença padrão da GreenSock
