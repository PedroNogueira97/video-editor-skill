# Histórico de decisões

Decisões técnicas da skill `editar-video`, com o contexto e o motivo de cada uma. Antes de propor trocar uma ferramenta ou abordagem, confira se ela já foi tentada aqui. Decisões novas entram no fim, numeradas e datadas.

Formato: **Contexto** (o problema) · **Decisão** · **Por quê** · **Consequências**.

---

## D01 — Revisão no navegador, não num editor de vídeo (2026-10)

**Contexto.** A primeira versão exportava para o DaVinci Resolve, e a segunda para o Kdenlive. O PC do usuário não tem GPU: o Resolve não roda, e o usuário não usa editor de vídeo.

**Decisão.** A revisão acontece numa `timeline.html` gerada pela skill, aberta no Chrome do Windows, e o render final sai por script (ffmpeg/libx264).

**Por quê.** É o único caminho que funciona só com CPU e sem ensinar um editor ao usuário.

**Consequências.** A skill mantém uma interface própria (`templates/timeline.html`). O export OTIO para o Kdenlive ficou como opcional (`--otio`), sem o layout.

## D02 — `projeto.json` como fonte única da verdade (2026-10)

**Contexto.** A timeline, o render, o OCR e a composição do Reels precisam concordar sobre o que foi cortado.

**Decisão.** Todo o estado da edição vive num JSON ao lado do vídeo. Tudo o mais (HTML, proxies, `index.html`, PNG do card) é derivado e pode ser refeito.

**Por quê.** O Claude e o usuário editam o mesmo arquivo, e qualquer script pode ser rodado de novo sem perder nada.

**Consequências.** Nunca edite o `index.html` ou o `timeline.html` gerados. O `cortes.py` recusa sobrescrever o projeto sem `--sobrescrever` (com `.bak`), e o `--aplicar-takes` existe para marcar takes sem refazer o projeto.

## D03 — Tempos no tempo original da câmera (2026-10)

**Decisão.** Blocos, takes, gancho, efeitos e sobreposições usam segundos do arquivo original da câmera. A exceção são os borrões, que ficam no tempo e nos pixels da tela, porque é na tela que eles são aplicados.

**Por quê.** O tempo final muda a cada corte. O tempo original é estável, e é nele que a transcrição está.

**Consequências.** Quando o usuário cita um tempo do vídeo final, o Claude precisa converter (`data-start`/`data-media-start` no Reels, `tempo_final()` no YouTube).

## D04 — Transcrição pela ElevenLabs Scribe, com cache (2026-10)

**Decisão.** `scribe_v2`, português, timestamps por palavra. A resposta bruta é salva em `<video>.transcricao.json` e reaproveitada.

**Por quê.** Os tempos por palavra são o que permite cortar silêncios com precisão, e a transcrição sai da CPU local, que já fica ocupada com os renders. O cache garante que cada vídeo seja pago uma vez só.

**Consequências.** Sem chave, o `--sem-api` cai para o `silencedetect` (sem transcrição, sem takes, sem legenda). A API às vezes estica uma palavra sobre um silêncio; `--palavra-max` confere essas palavras com o `silencedetect`.

## D05 — Takes repetidos são decididos pelo Claude, com aprovação (2026-10)

**Decisão.** Nenhum script detecta takes. O Claude lê a transcrição, propõe uma tabela e só grava o `takes.csv` depois do "ok". A regra é **manter o último take**.

**Por quê.** Reconhecer "vou falar de novo", frases recomeçadas e comentários de bastidor é um julgamento de linguagem, e errar corta conteúdo bom.

**Consequências.** Digressões e conteúdo polêmico não são takes: entram como sugestão à parte.

## D06 — Data do celular é a do fim da gravação (2026-10-09)

**Contexto.** No vídeo do CRM, a data de criação deixou câmera e tela fora de sincronia. O offset real, medido por eventos falados, foi 213,0 s.

**Decisão.** O `compute_offset()` detecta o padrão (tela "começa" antes da câmera quase a duração inteira dela) e soma a duração da câmera. O offset final é sempre conferido por eventos visíveis com folhas de contato.

**Por quê.** Celulares (Samsung) gravam o fim da gravação, enquanto o Mac grava o início, e os relógios ainda erram por alguns segundos.

## D07 — Render do YouTube trecho a trecho, com cache (2026-10)

**Decisão.** Cada trecho vira um `.mkv` em `render_cache/`, com nome pelo hash de entradas e filtros, e a junção final é feita com o `concat` copiando o vídeo.

**Por quê.** O render de 22 min levou ~1h45. Se ele parar, ou se o usuário mudar um bloco, só os trechos afetados são refeitos.

## D08 — Servidor próprio com suporte a Range (2026-10)

**Decisão.** O `--servir` usa um `ThreadingHTTPServer` próprio, com `206 Partial Content`, `POST /salvar` e `GET /projeto.json` lido do disco.

**Por quê.** O `python3 -m http.server` não atende Range, e sem isso o `<video>` do Chrome não pula de trecho. Abrir o HTML direto do disco funciona, mas aí o Salvar só consegue baixar o arquivo.

**Consequências.** O Salvar faz uma mescla (`mesclar()`): só os blocos e o `ativo` dos borrões vêm da página, para uma aba antiga não apagar borrões ou o offset gerados depois.

## D09 — OCR com RapidOCR na CPU, só na tela que entra no vídeo (2026-10)

**Decisão.** RapidOCR (modelos tiny, onnxruntime). Lê quadros só quando a tela muda, e só nos trechos usados, com cache de caixas e progresso em `ocr_tela.json`. Os achados começam **ativos**, e o usuário desmarca o que pode aparecer.

**Por quê.** OCR de 40 min de tela inteira na CPU seria inviável. Errar para o lado de borrar demais é mais seguro do que vazar um dado.

## D10 — Reels no HyperFrames (2026-10)

**Decisão.** O modo `reels` gera um projeto HyperFrames (HTML + GSAP), com preview no Studio e render por Chrome headless. A versão é fixada em 0.8.143 nos testes.

**Por quê.** Legenda karaokê, textos e zooms animados são simples em HTML/GSAP, o Studio dá ao usuário um preview no navegador (coerente com D01) e tudo roda só com CPU.

**Consequências.** O modo depende de Node 22 e do Chrome do HyperFrames. O GSAP e a fonte ficam em `templates/` e são copiados para o projeto.

## D11 — YuNet (OpenCV) em vez de MediaPipe para seguir o rosto (2026-10)

**Contexto.** O MediaPipe atual exige AES/AVX2 e aborta o processo no i3-2100.

**Decisão.** Detector YuNet do OpenCV (ONNX), amostrado a 5 fps. O caminho é suavizado (mediana, zona morta, ganho) e aplicado com `crop` dinâmico via `sendcmd`.

**Por quê.** O OpenCV escolhe as instruções da CPU em tempo de execução e roda em qualquer x86-64.

**Consequências.** `enquadramento.seguir_rosto: false` é a saída quando o rastreio balança ou corta o rosto.

## D12 — Render do Reels a partir do disco do WSL (2026-10-12)

**Contexto.** Lendo o `base.mp4` de `/mnt/e`, o Chrome headless do render travava.

**Decisão.** Copiar `reels_<video>/` para `~/hf/` e renderizar com `--pasta`.

**Consequências.** O render leva ~1,7 min por segundo de vídeo. O `hyperframes snapshot` estoura o timeout nesta máquina, então os quadros são conferidos com ffmpeg no MP4.

## D13 — Edição dinâmica declarativa no projeto (2026-10-10)

**Contexto.** O primeiro Reels de publi pediu zooms, sons, logo e textos ("edição que prende").

**Decisão.** Os efeitos viram dados em `projeto.json → "efeitos"` e `"titulo"`, com tempos do vídeo original batendo nas palavras fortes. O `reels_compor.py` traduz isso para GSAP e para clipes de áudio. Os sons vêm de uma biblioteca local (`templates/sfx/`, Pixabay, uso comercial liberado) com um `manifest.json` descritivo.

**Por quê.** Mantém D02 (nada se edita no `index.html`) e deixa o Claude escolher sons pelo sentido descrito no manifesto.

**Consequências.** Imagem com licença CC BY/BY-SA exige `credito`. Logo de marca só se for a da publi e confirmado com o usuário.

## D14 — Dependências inline com `uv` (2026-10)

**Decisão.** Cada script declara as próprias dependências (PEP 723) e roda com `uv run --script`. Não há `requirements.txt` nem venv do projeto.

**Por quê.** A skill é chamada de qualquer pasta, pelo Claude, e precisa funcionar sem passo de instalação.

**Consequências.** Script que importa outro declara o superconjunto das dependências.

## D15 — Testes com ElevenLabs mockada e caminhos com espaço (2026-10)

**Decisão.** O `teste_sintetico.sh` gera vídeos com ffmpeg e põe um SDK falso da ElevenLabs no `PYTHONPATH`, com contador de chamadas para provar o cache. A pasta padrão fica em `/mnt/c/Users/Public/teste editar video`.

**Por quê.** Testar de ponta a ponta sem custo, e pegar os erros de caminho do Windows (espaços, `/mnt`) que aparecem nos vídeos reais.

## D16 — Falsos começos `--` saem só da transcrição (2026-10-14)

**Contexto.** A ElevenLabs marca palavras abandonadas com `--` ("ti--"). O texto mandado ao n8n não deve ter gagueira.

**Decisão.** O `transcricao_limpa.py` remove o falso começo do texto: repetição das próximas 3–5 palavras numa janela de 20 s ou, se não houver, recuo até a pausa > 0,5 s, com limite de 6 palavras / 3 s (senão, remove só a palavra e marca `incerto`). Isso **nunca** altera o `projeto.json`. Retake que ainda está no vídeo vira sugestão de take, com aprovação (D05).

**Consequências.** O texto limpo sempre passa pelos cortes da timeline: ela também tira retakes sem `--`, que a regra não pega.

## D17 — Frames de capa com YuNet + Haar, OpenCV < 5 (2026-10-14)

**Decisão.** A nota de cada frame soma rosto grande (YuNet, 0,45), olhos abertos (Haar `haarcascade_eye`, 0,25) e nitidez (variância do Laplaciano, 0,30), com distância mínima entre os escolhidos. Os frames vêm só de trechos com a câmera em tela cheia, e são extraídos do arquivo original, sem legenda.

**Por quê.** O MediaPipe não roda nesta CPU (D11). O `CascadeClassifier` saiu do OpenCV 5.0, por isso o `enviar_n8n.py` fixa `opencv-python-headless<5`.

**Consequências.** O Haar não percebe mão na frente do rosto nem olho meio fechado: frames assim podem ganhar nota alta.

## D18 — Multipart com campos nomeados e só o que foi usado (2026-10-14)

**Decisão.** O envio ao n8n tem os campos `payload` (JSON), `frame_NN`, `referencia_thumb` e `asset_NN`, cada arquivo no seu campo. Vão os assets usados **nesta** edição: imagens inseridas, fonte do render, ícones do card e logos do `context.json` citados na fala. Sons vão só pelo nome. As pastas de assets servem para classificar (meme, logo, B-roll) e nunca são enviadas inteiras.

**Por quê.** Campos distintos viram propriedades binárias sem ambiguidade no Webhook node, e o n8n não enxerga o disco do WSL.

## D19 — `context.json` na raiz da skill, só para o n8n por enquanto (2026-10-14)

**Decisão.** Um arquivo por canal, fora do git, validado ao carregar. Asset ausente gera aviso, não erro. Contexto específico de um vídeo vem do pedido do usuário via `--nota`.

**Pendente (spec 002).** O usuário decidiu que o render deve passar a seguir o contexto: legenda do Reels em Typewriter/branca/sem destaque, cor de marca `#5BC8F5` no lugar do amarelo `#FFD400`. Até lá, o render continua lendo só o `projeto.json`.
