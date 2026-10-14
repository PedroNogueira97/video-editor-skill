# CLAUDE.md

Este repositório **é** a skill `editar-video`. No WSL, `~/.claude/skills/editar-video` é um symlink para ele, então qualquer mudança aqui vale na hora para o usuário. Há também um symlink em `.claude/skills/editar-video` para a skill funcionar quando o Claude Code é aberto nesta pasta.

- [SKILL.md](SKILL.md): o que o Claude faz quando a skill é invocada (fluxo, aprovações, problemas comuns).
- [SPECS.md](SPECS.md): arquitetura, schema do `projeto.json` e comportamento dos scripts.
- [README.md](README.md): instalação e uso.
- [.claude/archive/decisoes.md](.claude/archive/decisoes.md): decisões técnicas já tomadas e o porquê. Leia antes de propor trocar uma ferramenta ou abordagem.

## Ambiente

- Windows 10 + WSL Ubuntu, **sem GPU** (CPU i3-2100, sem AVX2). Nada que dependa de GPU ou de editor de vídeo desktop. O usuário aprova no Chrome do Windows.
- Rode tudo **dentro do WSL**. Se a sessão estiver no Windows: `wsl.exe -d Ubuntu --cd /home/pedro/vibecoding-projects/video-editor-skill -- bash -lc "..."`.
- O **git** também tem que rodar pelo WSL. Pelo caminho `\\wsl.localhost\...`, o git do Windows recusa o repositório ("dubious ownership").
- Sem `sudo` sem senha no WSL. Node 22 fica em `~/.local/node`.
- Remoto: `git@github.com:PedroNogueira97/video-editor-skill.git`, branch `master`.

## Comandos

```bash
uv run scripts/<script>.py --help          # cada script instala as próprias dependências
bash tests/teste_sintetico.sh              # ponta a ponta dos dois modos (ElevenLabs mockada); demora
uv run --script tests/teste_enquadramento.py /tmp/teste-enq
uv run --script tests/teste_n8n.py         # context.json, transcrição limpa, dry-run e envio
uv run scripts/contexto.py                 # valida o context.json
```

Rode o `teste_sintetico.sh` depois de mexer em `cortes.py`, `youtube_timeline.py`, `reels_compor.py` ou `templates/timeline.html`.

## Convenções

- Código, nomes, mensagens, comentários e commits **em português**, no tom dos scripts atuais: mensagens curtas com `→` para progresso, `✔` para concluído e `⚠` para aviso.
- Cada script começa com o bloco PEP 723 (`# /// script`) e uma docstring com "O que faz" e "Uso". Se um script importa outro, declare as dependências dos dois.
- Só stdlib, numpy e as libs já usadas. Chamadas a ffmpeg/ffprobe via `subprocess`, com a função `run()` local.
- O **`projeto.json` é a fonte única da verdade**. Recurso novo vira campo no projeto (com padrão no `cortes.py` quando fizer sentido), nunca estado escondido em arquivo gerado. Tempos no **tempo original da câmera**; os borrões são a exceção (tempo da tela).
- `timeline.html` (na pasta do vídeo), `index.html` do HyperFrames, `base.mp4` e o card são **gerados**. Corrija o gerador, não a saída.
- Nunca destrua edições do usuário: mantenha o `.bak` antes de sobrescrever e o `mesclar()` do Salvar só altera blocos e `ativo` dos borrões.
- Nunca imprima nem logue a `ELEVENLABS_API_KEY` nem o token do n8n. O `.env` está no `.gitignore`.
- `context.json` (identidade do canal) e `assets/` (referências, logos, memes) **nunca** vão para o git; só o `context.example.json`, com valores genéricos. Transcrições e projetos de vídeos reais também não, nem como fixture: os testes usam dados sintéticos (o repo é público).
- Funcionalidade nova começa por uma spec em `specs/` (decisões + dúvidas) e espera aprovação antes do código.

## Ao mudar uma flag ou um campo do projeto

Mantenha os quatro em sincronia:

1. `argparse` / código;
2. seção **Flags** e tabela **Problemas comuns** do `SKILL.md`;
3. tabelas de flags do `README.md`;
4. schema e comportamento no `SPECS.md`.

Se a mudança vier de uma escolha técnica que não é óbvia (por que X e não Y), registre em `.claude/archive/decisoes.md`.

## Sobre os vídeos do usuário

- Reels vêm do celular **já verticais**. O ffprobe mostra 1920x1080 por causa da flag de rotação. Confira `camera.largura/altura` no projeto antes de falar em orientação.
- Vídeos ficam em `/mnt/c` e `/mnt/e`. Para renderizar Reels, copie a pasta para o disco do WSL (`~/hf/`), porque lendo de `/mnt/*` o Chrome trava.
- Renders levam horas nesta CPU: rode em segundo plano e não dispare dois ao mesmo tempo.
