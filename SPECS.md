# SPECS — editar-video

Especificação técnica da skill `editar-video`: arquitetura, contrato do `projeto.json`, comportamento de cada script e limites conhecidos. Para instalação e uso, veja o [README.md](README.md). Para o fluxo que o Claude segue, veja o [SKILL.md](SKILL.md).

## 1. Objetivo e escopo

Fazer o primeiro corte de vídeos falados, sem editor de vídeo e sem GPU:

- tirar silêncios e takes repetidos;
- no modo `youtube`, combinar câmera e gravação de tela, borrar dados sigilosos e sobrepor o card das redes;
- no modo `reels`, reenquadrar para 1080x1920, legendar em karaokê e aplicar efeitos (zoom, som, imagem, texto);
- deixar a revisão no navegador e o render final por script.

Fora do escopo: correção de cor, mixagem de áudio, trilha sonora, transições entre cenas e edição multicâmera.

## 2. Ambiente-alvo

| Item | Valor |
|---|---|
| SO | Linux; desenvolvido e testado no WSL Ubuntu (Windows 10) |
| Hardware | só CPU; referência: i3-2100 (sem AVX2/AES-NI) |
| Python | ≥ 3.9, dependências inline (PEP 723), executado com `uv run --script` |
| Binários | `ffmpeg`, `ffprobe` |
| Reels | Node 22, HyperFrames 0.8.143 (CLI via `npx`), Chrome headless do HyperFrames |
| Transcrição | ElevenLabs Speech-to-Text, `scribe_v2`, `language_code="por"`, `timestamps_granularity="word"` |
| Revisão | Chrome do Windows acessando o `localhost` do WSL |

Arquivos de mídia geralmente ficam em `/mnt/<drive>/...`. Os scripts gravam o caminho Linux e o caminho Windows (`wslpath -w`) no projeto.

## 3. Arquitetura

```
transcrever.py ──▶ <video>.transcricao.json (cache)  +  transcricao.txt
       ▲
       │ import
cortes.py ───────▶ projeto.json  (+ trechos_tela.csv)
                        │
        ┌───────────────┼──────────────────────┐
        ▼               ▼                      ▼
   sigilo.py    youtube_timeline.py      reels_compor.py
  (borroes)     proxies/, timeline.html,  reels_<video>/ (HyperFrames)
                servidor, render_cache/,  reels_final.mp4
                video_final.mp4, .otio
```

Princípios:

1. **Fonte única da verdade.** Todo estado da edição vive no `projeto.json`. Os artefatos (`timeline.html`, `index.html` do HyperFrames, PNG do card, `base.mp4`) são derivados e podem ser refeitos a qualquer momento.
2. **Tempo original da câmera.** Todos os tempos no projeto estão em segundos do arquivo da câmera, exceto os borrões, que estão no tempo da tela. A conversão para o tempo do vídeo final acontece na hora de compor ou renderizar.
3. **Idempotência e cache.** A transcrição é paga uma vez. Proxies, `base.mp4`, trechos renderizados e leituras de OCR são cacheados por hash ou por chave e reaproveitados.
4. **Aprovação humana.** Os scripts não decidem takes nem conteúdo. O Claude propõe a lista e o usuário aprova antes de qualquer gravação no projeto.

Os scripts importam funções uns dos outros (`import transcrever`, `from cortes import ...`). Por isso, todos declaram o superconjunto de dependências que precisam.

## 4. `projeto.json`

Gerado pelo `cortes.py` e gravado com `indent=1` e `ensure_ascii=False`, com os blocos sempre ordenados por `inicio`. `versao` é `1`.

### 4.1 Campos comuns

```jsonc
{
 "versao": 1,
 "modo": "youtube" | "reels",
 "criado_em": "2026-10-09T10:00:00",
 "camera": Midia,
 "tela": Midia | null,                 // só youtube
 "transcricao": "/mnt/.../camera.mp4.transcricao.json" | null,
 "offset": 213.0,                      // tempo_camera = tempo_tela + offset
 "parametros": {"silencio_min": 0.7, "respiro": 0.15, "sem_api": false},
 "trechos_tela": [[12.0, 20.5], ...],  // tempo da câmera
 "takes": [{"inicio": 190.2, "fim": 198.95, "motivo": "..."}],
 "blocos": [Bloco, ...],
 "formato": {"largura": 1920, "altura": 1080, "fps": "30000/1001"}
}
```

`Midia`:

```jsonc
{"arquivo": "/mnt/e/...", "windows": "E:\\...", "duracao": 1834.2,
 "fps": "30/1", "largura": 1080, "altura": 1920,   // já com a rotação descontada
 "audio": true, "audio_rate": 48000, "audio_canais": 2,
 "criado": "2026-10-08T10:00:00+00:00" | null}
```

`Bloco`:

```jsonc
{"inicio": 12.34, "fim": 18.9, "tela": true, "removido": false, "motivo": ""}
```

- O intervalo entre dois blocos consecutivos é silêncio cortado.
- `removido: true` significa take descartado. O bloco continua no projeto para poder ser restaurado na timeline.
- `tela` só tem efeito no modo `youtube`.

### 4.2 Campos do modo `youtube`

```jsonc
"layout": {"escala_tela": 0.72, "escala_camera": 0.25, "margem": 24, "cor_fundo": "black"},
"borroes": [{
  "id": "b1", "tipo": "email|cpf|cnpj|telefone|token", "texto": "...", "ativo": true,
  "ocorrencias": [{"ini": 40.0, "fim": 55.0, "x": 1092, "y": 892, "w": 420, "h": 56}]
}],                                    // tempo e pixels da TELA original
"redes": {"handle": "@...", "plataformas": ["instagram", "tiktok", "youtube"],
          "chamada": "me segue nas redes", "cor": "#FFD400"} | null,
"sobreposicoes": [{"tipo": "redes", "inicio": 1.4, "fim": 9.5}]
```

### 4.3 Campos do modo `reels`

```jsonc
"formato": {"largura": 1080, "altura": 1920, "fps": "30/1"},
"legenda": {"palavras_max": 3, "fonte_px": 84, "cor": "#FFFFFF", "destaque": "#FFD400",
            "contorno": "#000000", "posicao_y": 0.62, "caixa_alta": true,
            "palavras_chave": [], "cor_chave": "#FF2E93"},
"gancho": {"inicio": 12.4, "fim": 15.1} | null,
"enquadramento": {"seguir_rosto": true},          // padrão: true se a câmera é horizontal
"titulo": {"texto": "... *destaque* ...", "y": 0.11, "destaque": "#E53935",
           "fundo": "#FFFFFF", "cor": "#111111", "fonte_px": 66},
"efeitos": {
  "zooms":   [{"inicio", "fim", "escala": 1.2, "estilo": "punch|suave|impacto", "de": 1, "foco_y": 0.35}],
  "sons":    [{"t", "som": "<chave do manifest>" | "arquivo": "/caminho", "volume": 0.4}],
  "imagens": [{"inicio", "fim", "arquivo", "largura": 720, "y": 0.14, "fundo", "rotacao", "raio", "credito"}],
  "textos":  [{"inicio", "fim", "texto", "y": 0.16, "cor": "#FFFFFF", "fundo": "#FF007E", "rotacao": -3}]
}
```

### 4.4 Regras de escrita

- O `cortes.py` recusa sobrescrever um projeto existente, a não ser com `--sobrescrever`, e nesse caso faz backup em `projeto.json.bak`.
- `--aplicar-takes` só divide e marca blocos. As outras edições são preservadas.
- O `POST /salvar` da timeline só altera `blocos` e o `ativo` de cada borrão. O resto vem do disco (veja `mesclar()`), para que uma aba aberta há tempo não apague o que foi gerado depois, como borrões novos ou o offset.

## 5. Scripts

### 5.1 `transcrever.py`

| | |
|---|---|
| Entrada | vídeo da câmera |
| Saída | `<video>.transcricao.json` (resposta bruta da API) e `transcricao.txt` (`[HH:MM:SS.mmm → HH:MM:SS.mmm] frase`) |
| Processo | extrai o áudio (mono, 64 kbps) → `speech_to_text.convert` (timeout de 3600 s) → cache |
| Cache | se o JSON existe, a API não é chamada; `--refazer` força uma nova chamada |
| Frases | quebra em `. ? ! …` ou em pausa maior que 0,7 s |
| Chave | `ELEVENLABS_API_KEY` no ambiente ou no `.env` da raiz da skill; nunca é impressa |

### 5.2 `cortes.py`

1. **Probe** (`ffprobe`): duração, fps (encaixado em taxas padrão: 23.976 a 60), dimensões com rotação aplicada, áudio e `creation_time`.
2. **Fala**:
   - Com API: os intervalos de fala vêm das palavras. Uma pausa maior ou igual a `--silencio-min` vira corte, e o `--respiro` é mantido antes e depois. Palavras com mais de `--palavra-max` s são conferidas com o `silencedetect` (`--ruido` dB), porque a API às vezes estica uma palavra sobre o silêncio.
   - Com `--sem-api`: usa só o `silencedetect`.
3. **Offset** (youtube): `--offset` manual ou diferença de `creation_time`. Se a tela parece ter começado antes da câmera quase a duração inteira dela (delta < −60 s e delta + duração da câmera entre −60 e 900 s), assume que o celular gravou a data do *fim* e soma a duração. Avisa quando |delta| > 600 s.
4. **Atividade da tela** (youtube): decodifica a tela a 2 fps em 96x54 cinza e calcula a diferença média entre quadros. Um segundo é ativo se o pico passa de `--sensibilidade`. Trechos separados por menos de `--juntar` s são unidos, trechos menores que `--tela-min` s são descartados, e cada trecho ganha `--folga-tela` s de cada lado antes de ser convertido para o tempo da câmera. O resultado também é salvo em `trechos_tela.csv` (editável e reutilizável com `--tela-csv`).
5. **Blocos**: interseção da fala com os trechos de tela, com bordas alinhadas a frames.
6. **Takes** (`inicio,fim,motivo`): divide os blocos nas bordas dos takes e marca como `removido` o pedaço cujo ponto médio cai num take. Sobras com menos de `--clipe-min` s coladas num take também são removidas, para não piscarem na tela.

### 5.3 `youtube_timeline.py`

**Proxies**: `proxies/*_480p.mp4`, com `scale=-2:480`, libx264 veryfast, CRF 28, GOP de 30 e AAC mono de 96 kbps na câmera.

**`timeline.html`**: gerado a partir de `templates/timeline.html`, com o projeto, as frases e os caminhos embutidos. É um arquivo único que abre direto do disco, e nesse caso o Salvar baixa o `projeto.json`.

**Servidor** (`--servir`, `ThreadingHTTPServer`, padrão `127.0.0.1:8765`):

| Rota | Comportamento |
|---|---|
| `GET /projeto.json` | sempre lê do disco |
| `GET /<arquivo>` com `Range` | `206 Partial Content`, necessário para o `<video>` pular de trecho |
| `POST /salvar` | valida os blocos (`inicio < fim`, `tela` e `removido` booleanos), grava `.bak`, faz `mesclar()` e salva |

Os cabeçalhos são `Accept-Ranges: bytes` e `Cache-Control: no-store`.

**Render** (`--renderizar`):

- Lista de edição em frames da câmera: `(frame_inicial, n_frames, inicio_na_tela | None)`. O OTIO usa a mesma lista.
- Quadro de 1920x1080.
  - Com tela: tela escalada para `escala_tela` no canto superior esquerdo (com `margem`) e câmera escalada para `escala_camera` no canto inferior direito, sobre um fundo `cor_fundo`.
  - Sem tela: câmera em tela cheia, com letterbox.
- Borrões: `boxblur` forte, recortado nas caixas e com janela de tempo própria, aplicado à tela **antes** de escalar.
- Card das redes: PNG transparente renderizado pelo Chrome headless do HyperFrames (Montserrat e os ícones de `templates/icones/`), sobreposto nas janelas de `sobreposicoes` convertidas para o tempo final.
- Cada trecho vira `render_cache/<sha1>.mkv` (libx264 com `--preset`/`--crf` e PCM 48 kHz estéreo). A chave do hash inclui arquivos, frames, grafo de filtros, card, CRF e preset, então só o que mudou é refeito.
- A junção final usa o demuxer `concat`, com vídeo copiado e AAC de 192 kbps, gerando `video_final.mp4`.
- Câmera sem áudio recebe `anullsrc`.

**OTIO** (`--otio`): trilhas V1 (câmera), V2 (tela) e A1 (áudio), para o Kdenlive 25.04+. O layout não é exportado.

### 5.4 `sigilo.py`

1. Considera só as janelas da tela que entram no vídeo (blocos ativos com `tela`).
2. Escolhe quadros pelo proxy: lê um quadro quando a mudança passa de `--limiar` e, de qualquer forma, pelo menos um a cada `--intervalo-max` s.
3. Faz o OCR no quadro original com RapidOCR (modelos tiny, onnxruntime, CPU). Caixas já vistas vêm do cache. O progresso fica em `ocr_tela.json`, então dá para interromper e retomar.
4. Classifica o texto por regex:

   | Tipo | Padrão |
   |---|---|
   | e-mail | `x@y` |
   | CPF | `000.000.000-00` |
   | CNPJ | `00.000.000/0000-00` |
   | telefone | BR, com ou sem +55 e com ou sem o 9 |
   | token | 28 ou mais caracteres `[A-Za-z0-9_-]`, com letra e dígito |

5. Agrupa as ocorrências por tipo e texto normalizado. Cada achado vale do quadro lido anterior até o seguinte (critério conservador). Caixas com IoU > 0,3 contíguas no tempo são unidas, e cada caixa ganha `--margem` px. Os borrões são gravados com `ativo: true`.

### 5.5 `reels_compor.py`

1. **`assets/base.mp4`**: o vídeo inteiro, sem cortes, em 1080x1920 a 30 fps.
   - Vertical: `scale` + `crop`.
   - Horizontal com `seguir_rosto`: o detector YuNet (ONNX, baixado do `opencv_zoo`) amostra o rosto a 5 fps. O caminho passa por interpolação, mediana de 5 amostras e zona morta de 6 % com ganho de 0,25, e é aplicado como `crop` dinâmico via `sendcmd` (`assets/crop.cmd`).
   - Cacheado por chave em `assets/base.json`.
2. **Segmentos da timeline**: o gancho (se houver) seguido dos blocos ativos em ordem. O gancho toca no início **e** continua no lugar original.
3. **Legenda**: palavras da transcrição mapeadas para o tempo final (pelo ponto médio da palavra). Os grupos têm até `palavras_max` palavras e quebram em pontuação ou em pausa maior que 0,6 s. Cada grupo some até 0,5 s depois da última palavra. A palavra ativa recebe `destaque` e escala; as palavras-chave recebem `cor_chave` e crescem mais.
4. **Efeitos** (`compor_efeitos`): os tempos são convertidos por `para_timeline()`. Um tempo que cai num corte vai para o começo do trecho seguinte; um tempo fora do vídeo descarta o efeito.

   | Efeito | Implementação |
   |---|---|
   | Zoom | GSAP no `.video`: `set` (punch), `fromTo` sine (suave), `set` + tremida determinística de 5 passos (impacto); volta a 1.0 no `fim` |
   | Som | `<audio class="clip">` nas trilhas 3 a 5, arquivo copiado para `assets/sfx/` |
   | Imagem | entra com `back.out`, flutua e sai; vai para `assets/img/` |
   | Texto | entra com pop rotacionado; fonte de até 64 px, ajustada ao tamanho do texto |
   | Título | manchete fixa do começo ao fim; `*palavra*` sai em `destaque` |

5. **`index.html`**: um `<video>` por segmento (`data-start`, `data-media-start`, `data-duration`), legenda, efeitos e a timeline GSAP (`templates/vendor/gsap.min.js`). A área segura reserva 90 px à esquerda e 170 px à direita. O arquivo é **gerado**: nunca edite à mão.
6. **Lint** do HyperFrames depois de cada composição.
7. **Preview** (`--preview`): Studio em `localhost:<porta>`, padrão 3002.
8. **Render** (`--renderizar`): `npx hyperframes render --fps 30 --quality looks --no-browser-gpu --workers <n>` → `renders/bruto.mp4` → remux com AAC de 192 kbps, 48 kHz e `+faststart`. Se o vídeo não sair em H.264/yuv420p, é recodificado com libx264 CRF 18. O resultado é `reels_final.mp4`.

## 6. Recursos empacotados

| Caminho | Uso | Licença |
|---|---|---|
| `templates/timeline.html` | interface da timeline | do projeto |
| `templates/vendor/gsap.min.js` | animações do Reels | GreenSock standard |
| `templates/fontes/Montserrat.ttf` | legenda, card, textos | OFL |
| `templates/icones/*.svg` | card das redes | CC0 |
| `templates/sfx/*.mp3` + `manifest.json` | efeitos sonoros (`file`, `duration`, `description`) | Pixabay Content License |

## 7. Desempenho de referência (i3-2100, sem GPU)

| Etapa | Custo observado |
|---|---|
| Proxies 480p | alguns minutos por vídeo de 40 min |
| `sigilo.py` | 15 a 30 min por 40 min de vídeo |
| Render YouTube (x264 medium) | ~1h45 para um final de 22 min |
| Render Reels (HyperFrames) | ~1,7 min por segundo de vídeo (~2,5 h para 87 s) |

## 8. Limitações conhecidas

- O OCR não é exaustivo: formulários, e-mails e perfis precisam de revisão visual.
- A data de criação dos arquivos erra por segundos. O offset precisa ser conferido por eventos.
- O MediaPipe não roda em CPUs sem AVX2/AES; por isso o reenquadramento usa YuNet.
- Lendo `base.mp4` de `/mnt/*`, o Chrome do render trava. Renderize a partir do disco do WSL (`--pasta`).
- O `hyperframes snapshot` estoura o timeout nesta máquina. Confira quadros com ffmpeg no MP4.
- O ffprobe mostra vídeos de celular com flag de rotação como 1920x1080. O `cortes.py` já desconta a rotação, então confie em `camera.largura/altura`.

## 9. Testes

| Arquivo | Cobertura |
|---|---|
| `tests/teste_sintetico.sh` | ponta a ponta dos dois modos com vídeos `testsrc2` + tom de 440 Hz e SDK da ElevenLabs mockado (`PYTHONPATH=tests/mock`, contador de chamadas). Valida o projeto, a timeline no Chrome headless (console limpo, Salvar por download e servidor), o render do YouTube (duração e layout), o OTIO e o Reels (duração, H.264/AAC, 1080x1920, 30 fps). Pasta de trabalho com espaço no nome. |
| `tests/teste_enquadramento.py` | o YuNet carrega e roda; o recorte acompanha um "rosto" sintético em movimento. `ROSTO_VIDEO=<arquivo>` testa com um vídeo real. |
| `tests/navegador.mjs` | driver puppeteer-core usado pelo teste sintético |
