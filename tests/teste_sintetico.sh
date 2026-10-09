#!/usr/bin/env bash
# Teste de ponta a ponta dos modos youtube e reels, com vídeos sintéticos e a
# ElevenLabs mockada. Uso: tests/teste_sintetico.sh [pasta_de_trabalho]
# A pasta padrão fica em /mnt/c (com espaço no nome) para testar caminhos do Windows.
set -euo pipefail

AQUI="$(cd "$(dirname "$0")" && pwd)"
S="$AQUI/../scripts"
DIR="${1:-/mnt/c/Users/Public/teste editar video}"
rm -rf "$DIR" && mkdir -p "$DIR/reels" "$DIR/downloads"
cd "$DIR"

echo "== Gerando vídeos sintéticos em $DIR"
# Câmera 30s (horizontal): tom de 440 Hz só nos trechos de "fala".
FALA='between(t,0.5,5)+between(t,7,12)+between(t,12.3,16)+between(t,19,25)+between(t,25.5,29.5)'
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "testsrc2=s=1280x720:r=30:d=30" \
  -f lavfi -i "aevalsrc='0.5*sin(2*PI*440*t)*($FALA)':s=48000:d=30" \
  -c:v libx264 -preset ultrafast -c:a aac -shortest \
  -metadata creation_time=2026-10-08T10:00:00Z "camera teste.mp4"
# Tela 32s, sem áudio, começa 2s depois da câmera. Fundo parado; um bloco
# só se mexe entre 10s e 18s do tempo da tela (= 12s a 20s da câmera).
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "color=c=gray:s=1920x1080:r=30:d=32" \
  -f lavfi -i "color=c=white:s=400x400:r=30:d=32" \
  -filter_complex "[0][1]overlay=x='if(between(t,10,18),100+(t-10)*150,100)':y=300,drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text='contato fulano.teste@gmail.com':x=1100:y=900:fontsize=40:fontcolor=black" \
  -c:v libx264 -preset ultrafast -an \
  -metadata creation_time=2026-10-08T10:00:02Z "tela teste.mov"
cp "camera teste.mp4" "reels/video reels.mp4"

export PYTHONPATH="$AQUI/mock"          # usa o SDK falso no lugar do real
export ELEVENLABS_API_KEY="chave-falsa"
export MOCK_CONTADOR="$DIR/chamadas_api.txt"
py() { uv run --quiet --script "$S/$1" "${@:2}"; }

[ -d "$AQUI/node_modules/puppeteer-core" ] || (cd "$AQUI" && npm install --silent >/dev/null)
CHROME=$(cd /tmp && npx --yes hyperframes@0.8.143 browser path 2>/dev/null | tail -1)

# ================================================================ YOUTUBE
echo "== youtube: cortes.py (chama a API mockada)"
py cortes.py youtube "camera teste.mp4" "tela teste.mov"
cp projeto.json projeto_sem_takes.json

echo "== youtube: rodar de novo sem --sobrescrever deve recusar"
if py cortes.py youtube "camera teste.mp4" "tela teste.mov" 2>/dev/null; then
  echo "ERRO: sobrescreveu o projeto"; exit 1
fi

echo "== youtube: aplicar takes num projeto existente"
printf 'inicio,fim,motivo\n00:00:01.000,00:00:03.000,recomeço\n' > takes.csv
py cortes.py --aplicar-takes takes.csv --projeto projeto.json

echo "== youtube: proxies + timeline.html"
py youtube_timeline.py projeto.json

echo "== youtube: timeline.html no navegador headless (file://)"
node "$AQUI/navegador.mjs" "$CHROME" "file://$DIR/timeline.html" "$DIR/downloads" > nav_file.json
cat nav_file.json; echo

echo "== youtube: timeline pelo --servir (Salvar grava no projeto.json)"
py youtube_timeline.py projeto.json --servir --porta 8799 > servidor.log 2>&1 &
SERV=$!
trap 'kill $SERV 2>/dev/null || true' EXIT
for _ in $(seq 60); do curl -fs -o /dev/null http://127.0.0.1:8799/timeline.html && break; sleep 0.5; done
curl -s -o /dev/null -w '%{http_code}\n' -H 'Range: bytes=0-99' \
  http://127.0.0.1:8799/proxies/camera_480p.mp4 > range.txt
node "$AQUI/navegador.mjs" "$CHROME" "http://127.0.0.1:8799/timeline.html" "$DIR/downloads" > nav_http.json
cat nav_http.json; echo
kill $SERV; trap - EXIT

echo "== youtube: dados sigilosos (OCR) + card das redes"
py sigilo.py projeto.json --intervalo-max 8
python3 - <<'EOF'
import json
p = json.load(open("projeto.json"))
p["redes"] = {"handle": "@teste", "plataformas": ["instagram", "youtube"], "cor": "#FFD400"}
p["sobreposicoes"] = [{"tipo": "redes", "inicio": 0.5, "fim": 4.0}]
json.dump(p, open("projeto.json", "w"), ensure_ascii=False, indent=1)
EOF

echo "== youtube: render + OTIO"
py youtube_timeline.py projeto.json --renderizar --preset ultrafast
py youtube_timeline.py projeto.json --otio

# ================================================================== REELS
# SO_YOUTUBE=1 pula o modo reels (o render dele leva ~10 min nesta CPU)
if [ -z "${SO_YOUTUBE:-}" ]; then
echo "== reels: enquadramento vertical (detector de rosto + recorte que acompanha)"
# ROSTO_VIDEO=<vídeo com uma pessoa> testa também a detecção num rosto real
uv run --quiet --script "$AQUI/teste_enquadramento.py" "$DIR/enquadramento"

echo "== reels: cortes.py + gancho + composição"
py cortes.py reels "reels/video reels.mp4"
python3 - <<'EOF'
import json
p = json.load(open("reels/projeto.json"))
p["gancho"] = {"inicio": 19.0, "fim": 21.0}          # simula um gancho aprovado
p["legenda"]["fonte_px"] = 96                        # simula "legenda maior"
json.dump(p, open("reels/projeto.json", "w"), ensure_ascii=False, indent=1)
EOF
py reels_compor.py reels/projeto.json | tee reels_compor.log
echo "== reels: render"
py reels_compor.py reels/projeto.json --renderizar | tee reels_render.log
fi

# ============================================================== VALIDAÇÃO
echo "== Validando"
uv run --quiet --no-project --with opentimelineio python - <<'EOF'
import json, re, subprocess
from pathlib import Path
import opentimelineio as otio

def probe(path):
    return json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                                      "-show_streams", "-show_format", path],
                                     capture_output=True, text=True, check=True).stdout)

def ativos(p):
    return sum(b["fim"] - b["inicio"] for b in p["blocos"] if not b["removido"])

def pixel(mp4, t, x, y):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", mp4, "-frames:v", "1",
                          "-vf", f"crop=2:2:{x}:{y}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    return tuple(raw[:3])

# ---- projeto.json (youtube)
p0 = json.load(open("projeto_sem_takes.json"))
assert p0["modo"] == "youtube" and p0["camera"]["windows"].startswith("C:\\"), p0["camera"]
t0 = ativos(p0)
# fala esperada ≈ (5.05-0.35)+(15.85-6.85)+(29.55-18.85) ≈ 24.4s
assert 23.9 < t0 < 24.9, t0
a, b = p0["trechos_tela"][0]
assert 9 <= a <= 12.5 and 19.5 <= b <= 23, (a, b)          # movimento 12–20s ± folga
assert abs(p0["offset"] - 2.0) < 0.01
assert any(b["tela"] for b in p0["blocos"])
p1 = json.load(open("projeto.json.bak"))                   # antes do Salvar via servidor
assert abs((t0 - ativos(p1)) - 2.0) < 0.1, (t0, ativos(p1))  # take de 2s removido
rem = [b for b in p1["blocos"] if b["removido"]]
assert rem and rem[0]["motivo"] == "recomeço", rem
print(f"projeto youtube: {len(p0['blocos'])} blocos, {t0:.2f}s → com take {ativos(p1):.2f}s")

# ---- timeline.html + proxies
assert Path("timeline.html").exists()
assert probe("proxies/camera_480p.mp4")["streams"][0]["height"] == 480
for nome in ("nav_file.json", "nav_http.json"):
    nav = json.load(open(nome))
    assert not nav["erros"], (nome, nav["erros"])
    e = nav["estado"]
    assert e["blocos"] > 0 and e["frases"] > 0 and not e["erroVideo"], (nome, e)
    assert nav["tocou"] > 0, (nome, nav["tocou"])
    print(f"{nome}: sem erros no console; {e['blocos']} blocos, {e['frases']} frases; "
          f"status '{nav['status']}'")
assert "baixado" in json.load(open("nav_file.json"))["status"]
baixado = json.load(open("downloads/projeto.json"))
assert baixado["blocos"][0]["tela"] is True and baixado["blocos"][0]["removido"] is False
assert "salvo" in json.load(open("nav_http.json"))["status"]
salvo = json.load(open("projeto.json"))
assert salvo["blocos"][0]["tela"] is True and salvo["blocos"][0]["removido"] is False
assert salvo["takes"] == p1["takes"]
assert open("range.txt").read().strip() == "206"
print("Salvar: download (file://) e gravação direta (servidor) ok; Range 206 ok")

# ---- render youtube
v = probe("video_final.mp4")
vs = next(s for s in v["streams"] if s["codec_type"] == "video")
as_ = next(s for s in v["streams"] if s["codec_type"] == "audio")
esperado = ativos(salvo)
for s in (vs, as_):
    assert abs(float(s["duration"]) - esperado) < 0.1, (s["duration"], esperado)
assert (vs["codec_name"], vs["width"], vs["height"]) == ("h264", 1920, 1080)
# meio do primeiro bloco com tela dentro da gravação da tela: tela cinza no topo
t_out, alvo = 0.0, None
for b in salvo["blocos"]:
    if b["removido"]:
        continue
    if b["tela"] and b["inicio"] - salvo["offset"] >= 0 and alvo is None:
        alvo = t_out + (b["fim"] - b["inicio"]) / 2
    t_out += b["fim"] - b["inicio"]
px = pixel("video_final.mp4", alvo, 60, 700)
assert all(abs(c - 128) < 20 for c in px), px
assert max(pixel("video_final.mp4", alvo, 1900, 30)) < 30
print(f"render youtube: {float(vs['duration']):.2f}s (esperado {esperado:.2f}s); layout ok")

# ---- dados sigilosos: OCR achou o e-mail e o render borrou
final = json.load(open("projeto.json"))
emails = [b for b in final["borroes"] if b["tipo"] == "email"]
assert emails and "fulano.teste@gm" in emails[0]["texto"], final["borroes"]
oc = emails[0]["ocorrencias"][0]
print(f"OCR: {emails[0]['texto']!r} em x={oc['x']} y={oc['y']} ({oc['w']}x{oc['h']})")
esc = 1382 / 1920                                   # tela 1920x1080 → 1382 de largura

def desvio(mp4, t, x, y, w, h):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", mp4, "-frames:v", "1",
                          "-vf", f"crop={w}:{h}:{x}:{y},format=gray", "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    import statistics
    return statistics.pstdev(raw)

bx, by = int(24 + (oc["x"] + 10) * esc), int(24 + (oc["y"] + 6) * esc)
bw, bh = int((oc["w"] - 20) * esc) // 2 * 2, int((oc["h"] - 12) * esc) // 2 * 2
d = desvio("video_final.mp4", alvo, bx, by, bw, bh)
print(f"desvio do texto borrado no render: {d:.1f}")
assert d < 25, d                                    # texto nítido preto no cinza dá ~50+

# ---- card das redes: borda amarela no canto inferior esquerdo no começo
# (câmera 0.5–4.0s; com o take 1–3s removido, no vídeo final fica ~0.15–1.65s)
card = pixel("video_final.mp4", 0.9, 50, 1080 - 48 - 40)
print("card (borda):", card)
assert card[0] > 200 and card[1] > 170 and card[2] < 80, card
fora = pixel("video_final.mp4", 6.0, 50, 1080 - 48 - 40)            # já saiu
assert not (fora[0] > 200 and fora[1] > 170 and fora[2] < 80), fora

# ---- OTIO
tl = otio.adapters.read_from_file("timeline.otio")
assert [t.kind for t in tl.tracks] == ["Video", "Video", "Audio"]
for c in tl.find_clips():
    assert re.match(r"^C:\\", c.media_reference.target_url), c.media_reference.target_url
v1 = tl.tracks[0]
assert abs(v1.duration().to_seconds() - esperado) < 0.05
print("OTIO: V1/V2/A1, caminhos C:\\ ok")

import os, sys
if os.environ.get("SO_YOUTUBE"):
    assert Path("chamadas_api.txt").read_text() == "1", "cache da transcrição não funcionou"
    print("✔ TESTES DO YOUTUBE PASSARAM (reels pulado)")
    sys.exit(0)

# ---- reels
pr = json.load(open("reels/projeto.json"))
assert pr["modo"] == "reels" and pr["parametros"]["silencio_min"] == 0.35
html = Path("reels/reels_video reels/index.html").read_text()
n_clips = html.count('class="clip video"')
n_leg = html.count('class="clip leg"')
assert n_clips == sum(not b["removido"] for b in pr["blocos"]) + 1   # + gancho
assert n_leg >= 5 and "font-size: 96px" in html
assert 'data-tipo="gancho"' in html.split('class="clip video"')[1]   # gancho primeiro
assert "Lint do HyperFrames: ok" in open("reels_compor.log").read()
total_reels = round(ativos(pr) + 2.0, 1)
r = probe("reels/reels_final.mp4")
rv = next(s for s in r["streams"] if s["codec_type"] == "video")
ra = next(s for s in r["streams"] if s["codec_type"] == "audio")
assert (rv["codec_name"], rv["width"], rv["height"], rv["pix_fmt"]) == ("h264", 1080, 1920, "yuv420p"), rv
assert rv["r_frame_rate"] == "30/1", rv["r_frame_rate"]
assert ra["codec_name"] == "aac"
dr = float(r["format"]["duration"])
assert abs(dr - total_reels) < 0.15, (dr, total_reels)
print(f"render reels: {dr:.2f}s (esperado {total_reels:.2f}s), {n_clips} clipes, {n_leg} legendas")

assert Path("chamadas_api.txt").read_text() == "2", "cache da transcrição não funcionou"
print("✔ TODOS OS TESTES PASSARAM")
EOF
