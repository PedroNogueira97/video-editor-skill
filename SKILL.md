---
name: editar-video
description: Primeiro corte automático de vídeos do usuário, em dois modos — `youtube` (vídeo longo com câmera + gravação de tela, timeline no navegador e render no ffmpeg) e `reels` (vídeo curto vertical 1080x1920 com HyperFrames, legenda karaokê e gancho). Transcreve pela ElevenLabs, corta silêncios e takes repetidos, tudo guardado num projeto.json. Use quando o usuário pedir para editar, cortar, decupar, tirar silêncios, montar timeline, legendar ou renderizar vídeo para o YouTube, Reels ou Instagram.
---

# Editar vídeo (`youtube` ou `reels`)

O usuário invoca com o modo: `/editar-video youtube <pasta ou arquivos>` ou
`/editar-video reels <vídeo>`. **Se ele não disser o modo, pergunte** (vídeo
longo com tela → youtube; vídeo curto para Reels/Instagram → reels).

## Ambiente (não mude sem motivo)

- Windows + WSL Ubuntu, **sem GPU**: tudo roda em CPU. Os scripts rodam **no WSL**
  com `uv` (dependências se instalam sozinhas). Se esta sessão estiver no Windows,
  use `wsl -d Ubuntu -- bash -lc "..."`.
- Vídeos ficam em pastas do Windows: converta com `wslpath -u 'E:\...'` (fica
  `/mnt/e/...`) e devolva caminhos ao usuário com `wslpath -w`. Sempre use aspas.
- O usuário **não usa editor de vídeo**: ele aprova no navegador (Chrome do
  Windows) e o render final é feito pelos scripts.
- Chave da ElevenLabs: `ELEVENLABS_API_KEY` no ambiente ou no `.env` da skill.
  Nunca imprima o valor nem peça a chave no chat.
- Reels precisa de Node 22 (`~/.local/node`, já no `~/.profile`) e do HyperFrames
  (`npx hyperframes doctor` deve mostrar Node, FFmpeg e Chrome ok).

```
S=~/.claude/skills/editar-video/scripts
```

## Base comum (os dois modos)

1. **Achar os arquivos.** A câmera é o arquivo **com** áudio
   (`ffprobe -v error -show_entries stream=codec_type -of csv=p=0 arq`); a tela
   (só youtube) não tem áudio.

2. **Transcrever** (paga uma vez; depois usa o cache `<video>.transcricao.json`):
   ```
   uv run $S/transcrever.py "/mnt/.../camera.mp4"
   ```
   Gera `transcricao.txt` na pasta do vídeo: `[00:12:03.120 → 00:12:07.900] texto`,
   no **tempo original** da câmera.

3. **Takes repetidos (você faz, não o script).** Leia o `transcricao.txt` inteiro e
   procure: pedidos de refazer ("vou falar de novo", "de novo", "corta", "peraí",
   "deixa eu refazer"), frases recomeçadas (o mesmo começo dito duas vezes), frases
   abandonadas, comentários de bastidor ("ficou escuro", "caiu o microfone",
   "se não atrapalhar a edição"). Regra: **mantenha o último take**. O corte vai do
   início da primeira tentativa ruim até ~0,05s antes da primeira palavra do take
   bom. Para tempos precisos, use `start`/`end` das palavras no JSON de cache
   (`type == "word"`). **Mostre a lista e espere a aprovação**:

   | # | Corte | O que sai | Fica assim |
   |---|---|---|---|
   | 1 | 00:03:10.200 → 00:03:18.950 | "então o primeiro… vou falar de novo" | "Então o primeiro passo é…" |

   Conteúdo polêmico ou digressões não são takes: liste à parte, como sugestão.
   Para ajudar, `uv run $S/enviar_n8n.py --listar-retakes projeto.json` lista os
   falsos começos marcados com `--` pela API (só depois que o projeto existe; antes,
   procure `--` no `transcricao.txt`). Eles entram na tabela como qualquer take.
   Depois do "ok", grave `takes.csv` na pasta do vídeo (`inicio,fim,motivo`).

4. **Gerar o projeto.json** (fonte única da verdade: blocos de fala, takes
   removidos, trechos de tela, offset; timeline e render leem dele):
   ```
   uv run $S/cortes.py youtube "camera.mp4" "tela.mov" --takes takes.csv
   uv run $S/cortes.py reels "video.mp4" --takes takes.csv
   ```
   Se o projeto já existir, o script recusa (pode ter edições do usuário). Para
   marcar takes novos sem perder edições:
   `uv run $S/cortes.py --aplicar-takes takes.csv --projeto projeto.json`.
   `--sobrescrever` refaz do zero (guarda o antigo em `projeto.json.bak`).
   Se a pasta tiver vídeos dos dois modos, use `--saida` com nomes diferentes.

   Estrutura dos blocos: `{inicio, fim, tela, removido, motivo}` em segundos do
   tempo original. O espaço entre blocos é silêncio cortado. Para cortar algo a
   pedido do usuário, edite o projeto.json (marque `removido: true`, dividindo o
   bloco se precisar) ou use `--aplicar-takes`.

## Modo `youtube` (vídeo longo, ~40 min)

5. **Timeline no navegador:**
   ```
   uv run $S/youtube_timeline.py projeto.json --servir
   ```
   Gera proxies 480p em `proxies/` (CPU, alguns minutos na primeira vez),
   `timeline.html` e sobe `http://localhost:8765/timeline.html` (rode em segundo
   plano; o Salvar grava direto no projeto.json, com backup em `.bak`). Sem
   `--servir`, dá para abrir o `timeline.html` direto (`wslpath -w`), e o Salvar
   baixa o `projeto.json` — o usuário precisa substituir o da pasta.

   Na timeline o usuário: vê fala (verde), fala + tela (azul), takes removidos
   (vermelho), silêncios cortados (cinza); toca o preview já com o layout; arrasta
   as bordas do bloco selecionado; `T` liga/desliga tela; `Del`/`R`
   remove/restaura; `S` divide no cursor; `Ctrl+Z` desfaz; `Espaço`, `J/K/L`,
   setas; clica na transcrição para pular. Diga isso a ele em poucas linhas.

   **Sincronia fala ↔ tela.** As datas dos arquivos erram por segundos (e o
   celular grava a data do FIM da gravação — o cortes.py já desconta). Confira
   por eventos: ache na transcrição falas que marcam algo visível ("abriu",
   "enviar", "importo", "e foi") e faça folhas de contato da tela em volta
   (`ffmpeg -ss T -t 30 -i proxies/tela_480p.mp4 -vf "fps=1,scale=300:-2,
   drawtext=...tempo da câmera...,tile=6x5"`). Se a tela mostra a ação N s antes
   da fala, some N ao `"offset"` do projeto.json (tela atrasada: subtraia). Se a
   diferença for grande, refaça os trechos de tela: `cortes.py ... --offset X
   --sobrescrever` (só se o usuário ainda não editou a timeline).

   **Dados sigilosos na tela** (e-mail, telefone, CPF/CNPJ, tokens):
   ```
   uv run $S/sigilo.py projeto.json          # OCR na CPU; ~15–30 min num vídeo de 40 min
   uv run $S/sigilo.py projeto.json --so-listar
   ```
   Lê só a tela que entra no vídeo, grava `"borroes"` no projeto.json (todos
   ativos) e lista o que achou. **Mostre a lista ao usuário**: ele desmarca o que
   pode aparecer (na timeline, painel "Dados sigilosos", ou peça e você põe
   `"ativo": false`). A timeline mostra os borrões no preview e o render aplica.
   O OCR não é perfeito: peça para ele passar o olho nas telas com formulários,
   e-mails e perfis. Se ele restaurar takes, rode de novo (reaproveita o cache).

   **Card das redes sociais** (lower third no canto inferior esquerdo, entra
   deslizando). No projeto.json:
   ```
   "redes": {"handle": "@pedrinverso", "plataformas": ["instagram", "tiktok", "youtube"],
             "chamada": "me segue nas redes", "cor": "#FFD400"},
   "sobreposicoes": [{"tipo": "redes", "inicio": 1.4, "fim": 9.5}, ...]
   ```
   `inicio`/`fim` no tempo ORIGINAL da câmera: o começo do vídeo e quando ele
   menciona as redes (procure "Instagram", "TikTok", "me segue", "redes sociais"
   na transcrição). Ícones disponíveis em `templates/icones/` (instagram, tiktok,
   youtube, linkedin, x, github). O `youtube_timeline.py` gera `assets/redes.png`.

6. **Render** (só depois de ele aprovar; demora — CPU, x264 medium):
   ```
   uv run $S/youtube_timeline.py projeto.json --renderizar
   ```
   Lê o projeto.json, renderiza trecho a trecho em `render_cache/` (reaproveita o
   que não mudou; se parar, é só rodar de novo) e junta em `video_final.mp4`.
   Layout: tela grande à esquerda + câmera no canto inferior direito nos blocos com
   tela; câmera em tela cheia no resto; borrões e card das redes aplicados. Rode
   em segundo plano.

   O Salvar da timeline só altera blocos e o liga/desliga dos borrões; o resto
   do projeto.json (offset, borrões, redes) pode ser editado por você com a
   timeline aberta — peça para o usuário recarregar a página depois.

   Opcional: `--otio` exporta `timeline.otio` para o Kdenlive 25.04+ (V1 câmera,
   V2 tela, A1 áudio; o layout não vai junto).

## Modo `reels` (vídeo curto vertical, até ~90s)

5. **Gancho.** Pela transcrição, sugira se vale abrir com a frase mais forte do
   vídeo (2–4s, uma ideia completa e intrigante), com o timecode e o texto.
   **Pergunte antes de aplicar.** Se ele aceitar, grave no projeto.json:
   `"gancho": {"inicio": 12.4, "fim": 15.1}` (o trecho toca no começo e continua
   no lugar original).

6. **Compor e abrir o preview:**
   ```
   uv run $S/reels_compor.py projeto.json --preview
   ```
   Cria `reels_<video>/` (projeto HyperFrames) ao lado do vídeo:
   `assets/base.mp4` em 1080x1920 (vídeo horizontal é reenquadrado seguindo o
   rosto com o detector YuNet do OpenCV — o MediaPipe atual exige AES/AVX2 e aborta
   na CPU deste PC, um i3-2100), `index.html` com um clipe por trecho mantido, legenda
   karaokê (2–4 palavras, palavra falada destacada) na área segura do Reels, e
   roda o lint. Abre o Studio em `http://localhost:3002/#project/reels_<video>`
   (deixe rodando durante a revisão; `npx hyperframes preview --stop` encerra).

7. **Ajustes em texto.** O usuário pede ("corta a parte do 0:12", "legenda maior",
   "tira o gancho"); você edita o **projeto.json** e roda `reels_compor.py` de novo
   (o preview recarrega sozinho). Não edite o `index.html` à mão: ele é refeito.
   - Tempo que ele cita é do **vídeo final** (com gancho e cortes). Converta para
     o tempo original somando os trechos: veja os `data-start`/`data-media-start`
     dos `<video>` no `index.html` (`tempo_original = media_start + (t - data_start)`).
   - Legenda: `legenda.fonte_px` (84), `palavras_max` (3), `cor`, `destaque`
     (#FFD400), `contorno`, `posicao_y` (0.62 da altura), `caixa_alta`.
   - Enquadramento: `enquadramento.seguir_rosto` (false = centro fixo).
   - **Edição dinâmica (estilo yap)** — quando ele pedir zooms, sons, logo ou
     "edição que prende": monte `"efeitos"` no projeto.json, com tempos do vídeo
     **original** (palavras do JSON de cache), batendo nas palavras fortes:
     ```
     "efeitos": {
       "zooms":   [{"inicio": 7.54, "fim": 9.0, "escala": 1.35, "estilo": "impacto"}],
       "sons":    [{"t": 7.54, "som": "impact-bass-1", "volume": 0.45}],
       "imagens": [{"inicio": 11.4, "fim": 16, "arquivo": "/mnt/e/.../logo.svg", "largura": 720, "y": 0.11}],
       "textos":  [{"inicio": 52.2, "fim": 53.3, "texto": "1. se cadastra", "y": 0.13, "fundo": "#FF007E"}]
     }
     ```
     Zoom: `punch` (salto seco), `suave` (aproxima ao longo do trecho, `de`
     opcional), `impacto` (salto + tremida); volta a 1.0 no `fim`; `foco_y`
     (0.35) é a altura do rosto. Um zoom a cada 2–4s, alternando 1.0 e 1.1–1.35;
     não empilhe em cima de zoom que já está gravado no vídeo. Sons em
     `templates/sfx/` (Pixabay, uso comercial liberado — ver `manifest.json`):
     whoosh/whoosh-short nas viradas e B-rolls, pop em listas e textos,
     impact-bass em punchlines, notification/ping/sparkle/click/error/glitch
     conforme o sentido; volume 0.25–0.5 (abaixo da voz). Ou `"arquivo"` próprio.
     Legenda: `legenda.palavras_chave` (lista) ficam em `cor_chave` (#FF2E93) e
     crescem mais. Manchete fixa no topo (o assunto/gancho do vídeo inteiro):
     `"titulo": {"texto": "Programador pode ser de *esquerda?*", "y": 0.11}` —
     `*palavra*` sai em `destaque` (#E53935). Imagens aceitam `rotacao`, `raio` e
     `credito` (obrigatório para foto CC BY/BY-SA, ex.: Wikimedia Commons). Logos de marca: procure o SVG oficial no site da marca e
     confirme com o usuário; não ponha logo de marca que não é a da publi.
   - Ritmo: refaça com `cortes.py reels ... --silencio-min 0.25 --sobrescrever`
     (perde edições; avise antes).

8. **Render** (só depois de aprovado):
   ```
   uv run $S/reels_compor.py projeto.json --renderizar
   ```
   Gera `reels_final.mp4` (H.264 + AAC, 30fps, 1080x1920) na pasta do vídeo.
   Nesta CPU leva ~1,7 min por segundo de vídeo. **Renderize do disco do WSL**:
   lendo o `base.mp4` de /mnt/e o Chrome trava; copie a pasta (`rsync -a
   reels_<video>/ ~/hf/reels_<video>/`) e use `--pasta ~/hf/reels_<video>`. Feche
   previews antigos (`npx hyperframes preview --stop`) antes.
   Celular grava vertical com flag de rotação: o ffprobe mostra 1920x1080, mas o
   `cortes.py` já lê a rotação — confira em `camera.largura/altura` do projeto.

## Título, descrição e capa (n8n, os dois modos)

Depois do render aprovado, pergunte se ele quer gerar título/descrição (youtube)
ou legenda (reels) e a capa, e se há **frases de destaque**. Se ele der contexto
específico do vídeo no pedido ("é publi da X", "cita o cupom Y"), passe com `--nota`.

```
uv run $S/enviar_n8n.py projeto.json --dry-run      # primeira vez: confira a pasta gerada
uv run $S/enviar_n8n.py projeto.json --frase "..." --nota "..."
```

- Precisa do `context.json` na raiz da skill (se faltar, o script manda copiar o
  `context.example.json`) e do token na variável indicada em
  `global.n8n.token_env_var` (ambiente ou `.env`). Nunca imprima nem peça o token
  no chat.
- O texto enviado é a transcrição do vídeo **editado**, sem falsos começos `--`.
  Se o script avisar "⚠ AINDA NO VÍDEO", ofereça cortar esses trechos como takes
  (`cortes.py --aplicar-takes`, com aprovação) antes de renderizar de novo.
- A resposta é síncrona: mostre o título/legenda do `copy.md` e o caminho Windows
  da capa (`n8n_<projeto>/capa.*`); pergunte se quer gerar de novo com outras
  frases ou notas.

## Flags

**cortes.py** `modo camera [tela]`: `--takes CSV`, `--aplicar-takes CSV --projeto
P`, `--saida`, `--sobrescrever`, `--silencio-min` (0.7 youtube / 0.35 reels),
`--respiro` (0.15), `--sem-api` (silencedetect), `--ruido` (-35 dB), `--palavra-max`
(2.0), `--clipe-min` (0.25), `--refazer-transcricao`, `--offset`, `--tela-csv`,
`--sensibilidade` (0.8), `--juntar` (10), `--tela-min` (4), `--folga-tela` (1.5).

**youtube_timeline.py** `projeto.json`: `--servir` (`--porta` 8765, `--host`
127.0.0.1), `--renderizar [MP4]`, `--crf` (20), `--preset` (medium), `--otio [ARQ]`.

**reels_compor.py** `projeto.json`: `--preview` (`--porta` 3002), `--renderizar
[MP4]`, `--pasta`.

**transcrever.py** `camera [--txt caminho] [--refazer]`.

**enviar_n8n.py** `projeto.json`: `--dry-run`, `--frase` (repetível), `--nota`
(repetível), `--listar-retakes`, `--contexto`.

## Problemas comuns

| Sintoma | Ajuste |
|---|---|
| Cortes secos, comendo palavras | `--respiro 0.25` no cortes.py |
| Ritmo picotado / ainda sobram pausas | `--silencio-min` maior / menor |
| Pausa longa não foi cortada | a API esticou uma palavra sobre o silêncio; `--palavra-max 1.5` |
| Tela adiantada/atrasada | ajuste `"offset"` no projeto.json (ver "Sincronia fala ↔ tela"); tela adiantada → offset maior |
| Dado sigiloso não borrado | rode `sigilo.py` com `--intervalo-max 5`, ou adicione um item em `"borroes"` à mão (caixa em px da tela original, tempos da tela) |
| Borrão em algo que pode aparecer | desmarque no painel "Dados sigilosos" da timeline |
| Card das redes no lugar/tempo errado | edite `"sobreposicoes"` (tempos da câmera) e rode `youtube_timeline.py` de novo |
| Aviso "diferença maior que 10 min" | relógio de um aparelho errado; use `--offset` |
| Tela não detecta digitação | `--sensibilidade 0.4`; ou ligue a tela no bloco pela timeline (`T`) |
| Tela entra e sai demais | `--juntar 20` / `--tela-min 8` |
| Timeline: "Não consegui abrir proxies/…" | rode `youtube_timeline.py projeto.json` (gera proxies) |
| Vídeo não avança/pula na timeline servida | o servidor precisa de Range (o `--servir` tem); não use `python3 -m http.server` |
| Salvar não grava | sem `--servir` ele baixa o arquivo: substitua o projeto.json da pasta |
| localhost não abre no Chrome do Windows | `--host 0.0.0.0` (WSL sem localhostForwarding) |
| Render do YouTube parou | rode de novo: `render_cache/` reaproveita os trechos prontos |
| Reels: rosto cortado / quadro balançando | `enquadramento.seguir_rosto: false` (centro fixo) |
| Reels: legenda em cima de algo importante | `legenda.posicao_y` (0.55–0.7) |
| HyperFrames sem Chrome | `npx hyperframes browser ensure` (precisa de `unzip`: `sudo apt install unzip`) |
| Lint do HyperFrames com erro | leia a saída; o `index.html` é gerado — corrija o `reels_compor.py`, não o HTML |
| Erro sem `ELEVENLABS_API_KEY` | coloque no `.env` da skill, ou use `--sem-api` |

## Teste

`bash tests/teste_sintetico.sh` gera vídeos sintéticos, mocka a ElevenLabs e
valida os dois modos: projeto.json, timeline.html num Chrome headless (sem erros
no console, Salvar por download e pelo servidor), render do YouTube (duração e
layout), OTIO, composição e render do Reels (duração, H.264/AAC, 1080x1920, 30fps).
