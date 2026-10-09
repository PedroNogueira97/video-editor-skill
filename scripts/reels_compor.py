#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["numpy", "elevenlabs", "opencv-python-headless>=4.8"]
# ///
"""
reels_compor.py — modo reels: projeto.json → composição HyperFrames (1080x1920).

O que faz (a partir do projeto.json gerado pelo cortes.py no modo reels):
  1. Gera assets/base.mp4 vertical 1080x1920 a 30fps. Vídeo horizontal é
     reenquadrado acompanhando o rosto (detector YuNet do OpenCV, só CPU;
     o MediaPipe não roda na CPU deste PC); vertical é só ajustado ao quadro.
  2. Escreve index.html: um <video> por trecho mantido (cada corte aparece
     como clipe na linha do tempo do Studio), gancho opcional no começo e
     legenda karaokê palavra por palavra na área segura do Reels.
  3. Roda o lint do HyperFrames.
  4. --preview: abre o Studio no navegador (localhost) para aprovar.
  5. --renderizar: MP4 H.264 + AAC, 30fps, pronto para postar.

Ajustes (cortes, legenda, gancho) se fazem no projeto.json e rodando de novo:
o preview recarrega sozinho.

Uso:
  uv run reels_compor.py projeto.json
  uv run reels_compor.py projeto.json --preview
  uv run reels_compor.py projeto.json --renderizar
"""

import argparse
import hashlib
import html
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import numpy as np

import transcrever
from cortes import fmt_tc, load_projeto, run, windows_path

SKILL = Path(__file__).resolve().parent.parent
FPS = 30
W, H = 1080, 1920
MODELO_ROSTO = ("https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
                "face_detection_yunet/face_detection_yunet_2023mar.onnx")
SAFE = {"esquerda": 90, "direita": 170}     # px livres nas laterais (ícones à direita)


def npx(args, cwd, **kw):
    return subprocess.run(["npx", "--yes", "hyperframes@0.8.143", *args], cwd=cwd, **kw)


def q(t):
    """Alinha um tempo ao frame de 30fps (3 casas)."""
    return round(round(t * FPS) / FPS, 3)


# --------------------------------------------- 1. enquadramento vertical

def face_track(cam, amostras_fps=5):
    """Centro horizontal do rosto (0..1) ao longo do vídeo, ou None se falhar.

    Usa o YuNet do OpenCV. O MediaPipe atual exige AES/AVX2 e aborta o processo
    em CPUs antigas como a deste PC (i3-2100); o OpenCV escolhe as instruções
    em tempo de execução e roda em qualquer x86-64."""
    try:
        import cv2
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ OpenCV indisponível ({e}); usando o centro do quadro")
        return None
    modelo = Path.home() / ".cache" / "editar-video" / "face_detection_yunet_2023mar.onnx"
    if not modelo.exists():
        modelo.parent.mkdir(parents=True, exist_ok=True)
        print("  baixando o modelo de rosto YuNet (230 KB)...")
        run(["curl", "-fsSL", "-o", str(modelo), MODELO_ROSTO])

    sw = 640
    sh = int(round(cam["height"] * sw / cam["width"] / 2)) * 2
    try:
        det = cv2.FaceDetectorYN.create(str(modelo), "", (sw, sh), 0.6)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ detector de rosto não carregou ({e}); usando o centro do quadro")
        return None
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(cam["path"]),
         "-vf", f"fps={amostras_fps},scale={sw}:{sh}", "-f", "rawvideo",
         "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE)
    xs, n = [], sw * sh * 3
    while True:
        buf = proc.stdout.read(n)
        if len(buf) < n:
            break
        frame = np.frombuffer(buf, np.uint8).reshape(sh, sw, 3)
        _, faces = det.detect(frame)
        if faces is not None and len(faces):
            x, _, w, _ = max(faces, key=lambda f: f[2] * f[3])[:4]   # o maior rosto
            xs.append((x + w / 2) / sw)
        else:
            xs.append(np.nan)
    proc.wait()
    xs = np.array(xs, dtype=float)
    achados = np.count_nonzero(~np.isnan(xs))
    print(f"  rosto encontrado em {achados}/{len(xs)} amostras")
    if achados == 0:
        return None
    return np.arange(len(xs)) / amostras_fps, xs


def smooth_path(ts, xs, zona_morta=0.06, ganho=0.25):
    """Preenche buracos, tira tremidas (mediana de 1s) e só move o quadro quando
    o rosto sai de uma zona morta, com aceleração suave."""
    idx = np.arange(len(xs))
    ok = ~np.isnan(xs)
    xs = np.interp(idx, idx[ok], xs[ok])
    k = 5
    pad = np.pad(xs, k // 2, mode="edge")
    med = np.array([np.median(pad[i:i + k]) for i in range(len(xs))])
    out, c = [], med[0]
    for x in med:
        if abs(x - c) > zona_morta:
            c += (x - c) * ganho
        out.append(c)
    return ts, np.array(out)


def make_base(cam, projeto, pasta_hf, seguir_rosto):
    """assets/base.mp4: o vídeo inteiro (sem cortes) já em 1080x1920 a 30fps."""
    assets = pasta_hf / "assets"
    assets.mkdir(exist_ok=True)
    base = assets / "base.mp4"
    chave = hashlib.sha1(json.dumps([str(cam["path"]), cam["duration"],
                                     seguir_rosto, 2]).encode()).hexdigest()[:16]
    marca = assets / "base.json"
    if base.exists() and marca.exists() and json.loads(marca.read_text())["chave"] == chave:
        print("→ assets/base.mp4 já está pronto")
        return base

    horizontal = cam["width"] > cam["height"] * 9 / 16 + 2
    vf = []
    if horizontal:
        cw = int(round(cam["height"] * 9 / 16 / 2)) * 2
        x0 = (cam["width"] - cw) // 2
        caminho = None
        if seguir_rosto:
            print("→ Procurando o rosto para reenquadrar (YuNet/OpenCV, CPU)...", flush=True)
            track = face_track(cam)
            if track:
                caminho = smooth_path(*track)
        if caminho:
            ts, cs = caminho
            frames = np.arange(0, cam["duration"], 1 / FPS)
            xs = np.interp(frames, ts, cs) * cam["width"] - cw / 2
            xs = np.clip(np.round(xs / 2) * 2, 0, cam["width"] - cw).astype(int)
            linhas, ultimo = [], None
            for t, x in zip(frames, xs):
                if x != ultimo:
                    linhas.append(f"{t:.4f} crop x {x};")
                    ultimo = x
            (assets / "crop.cmd").write_text("\n".join(linhas) + "\n")
            x0 = int(xs[0])
            vf.append("sendcmd=f=assets/crop.cmd")
        vf.append(f"crop={cw}:{cam['height']}:{x0}:0")
    else:
        vf.append(f"scale={W}:{H}:force_original_aspect_ratio=increase")
        vf.append(f"crop={W}:{H}")
    vf += [f"scale={W}:{H}", f"fps={FPS}", "setsar=1", "format=yuv420p"]

    print("→ Gerando assets/base.mp4 (1080x1920, 30fps)...", flush=True)
    t0 = time.time()
    tmp = assets / "base.tmp.mp4"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(cam["path"]),
           "-vf", ",".join(vf), "-c:v", "libx264", "-preset", "veryfast", "-crf", "17",
           "-g", str(FPS // 2), "-r", str(FPS)]
    cmd += (["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
            if cam["has_audio"] else ["-an"])
    run(cmd + ["-movflags", "+faststart", "assets/base.tmp.mp4"], cwd=pasta_hf)
    tmp.rename(base)
    marca.write_text(json.dumps({"chave": chave}))
    print(f"  pronto em {fmt_tc(time.time() - t0)[:8]}")
    return base


# ---------------------------------------------- 2. trechos e legendas

def segmentos(projeto):
    """Trechos na ordem da timeline: gancho (se houver) + blocos ativos."""
    segs = []
    g = projeto.get("gancho")
    if g:
        segs.append((q(g["inicio"]), q(g["fim"]), "gancho"))
    for b in sorted(projeto["blocos"], key=lambda b: b["inicio"]):
        if not b["removido"]:
            a, z = q(b["inicio"]), q(b["fim"])
            if z > a:
                segs.append((a, z, "fala"))
    return segs


def palavras_na_timeline(projeto, segs):
    """Palavras com tempo da timeline final (só as que caem num trecho mantido)."""
    if not projeto.get("transcricao") or not Path(projeto["transcricao"]).exists():
        return []
    words = transcrever.words_of(json.loads(
        Path(projeto["transcricao"]).read_text(encoding="utf-8")))
    out, t0 = [], 0.0
    for a, z, _ in segs:
        for w in words:
            meio = (w["start"] + w["end"]) / 2
            if a <= meio < z:
                out.append({"texto": w["text"].strip(),
                            "ini": t0 + max(w["start"], a) - a,
                            "fim": t0 + min(w["end"], z) - a})
        t0 += z - a
    return out


def grupos_legenda(palavras, maximo, total):
    """Agrupa 2–4 palavras por vez: quebra no máximo, em pontuação ou pausa."""
    grupos, cur = [], []
    for w in palavras:
        if cur and (len(cur) >= maximo or w["ini"] - cur[-1]["fim"] > 0.6
                    or cur[-1]["texto"].endswith((".", "?", "!", ":", ";"))):
            grupos.append(cur)
            cur = []
        cur.append(w)
    if cur:
        grupos.append(cur)
    out = []
    for i, g in enumerate(grupos):
        ini = g[0]["ini"]
        prox = grupos[i + 1][0]["ini"] if i + 1 < len(grupos) else total
        fim = min(prox, g[-1]["fim"] + 0.5, total)
        out.append({"ini": q(ini), "fim": q(max(fim, ini + 0.3)), "palavras": g})
    return out


# ------------------------------------------------------- 3. index.html

def compor_html(projeto, segs, grupos, total):
    L = projeto["legenda"]
    clips, t = [], 0.0
    for i, (a, z, tipo) in enumerate(segs):
        d = round(z - a, 3)
        clips.append(
            f'      <video id="c{i}" class="clip video" src="assets/base.mp4" playsinline\n'
            f'        data-has-audio="true" data-start="{round(t, 3)}" data-duration="{d}"\n'
            f'        data-media-start="{a}" data-track-index="0" data-volume="1"'
            f' data-tipo="{tipo}"></video>')
        t += d

    legs, anim = [], []
    for gi, g in enumerate(grupos):
        spans = " ".join(f'<span class="w" id="g{gi}w{wi}">{html.escape(w["texto"])}</span>'
                         for wi, w in enumerate(g["palavras"]))
        legs.append(f'      <div id="g{gi}" class="clip leg" data-start="{g["ini"]}" '
                    f'data-duration="{round(g["fim"] - g["ini"], 3)}" data-track-index="1">'
                    f'<p class="leg-in">{spans}</p></div>')
        anim.append(f'  tl.fromTo("#g{gi} .leg-in", {{scale: 0.82, opacity: 0}}, '
                    f'{{scale: 1, opacity: 1, duration: 0.12, ease: "back.out(2)"}}, {g["ini"]});')
        for wi, w in enumerate(g["palavras"]):
            ini = max(round(w["ini"], 3), g["ini"])
            anim.append(f'  tl.set("#g{gi}w{wi}", {{color: DESTAQUE, scale: 1.1}}, {ini});')
            anim.append(f'  tl.set("#g{gi}w{wi}", {{color: COR, scale: 1}}, '
                        f'{round(min(w["fim"], g["fim"]), 3)});')

    centro = round(L["posicao_y"] * H)
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width={W}, height={H}" />
  <title>{html.escape(Path(projeto["camera"]["arquivo"]).stem)}</title>
  <!-- gerado por reels_compor.py a partir do projeto.json: ajuste lá e rode de novo -->
  <script src="assets/gsap.min.js"></script>
  <style>
    @font-face {{
      font-family: "Legenda";
      src: url("assets/fontes/Montserrat.ttf") format("truetype");
      font-weight: 100 900;
    }}
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    html, body {{ width: {W}px; height: {H}px; overflow: hidden; background: #000; }}
    #root {{ position: relative; width: 100%; height: 100%; overflow: hidden; }}
    .video {{ position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }}
    .leg {{ position: absolute; left: {SAFE["esquerda"]}px; right: {SAFE["direita"]}px;
            top: {centro - 220}px; height: 440px; display: flex; align-items: center;
            justify-content: center; z-index: 5; }}
    .leg-in {{ display: block; width: 100%; text-align: center;
               font-family: "Legenda", sans-serif; font-weight: 900;
               font-size: {L["fonte_px"]}px; line-height: 1.12;
               color: {L["cor"]}; {"text-transform: uppercase;" if L["caixa_alta"] else ""}
               -webkit-text-stroke: {max(4, L["fonte_px"] // 12)}px {L["contorno"]};
               paint-order: stroke fill;
               text-shadow: 0 6px 18px rgba(0, 0, 0, 0.55); }}
    .w {{ display: inline-block; }}
  </style>
</head>
<body>
  <div id="root" data-composition-id="main" data-start="0"
       data-duration="{round(total, 3)}" data-width="{W}" data-height="{H}">
{chr(10).join(clips)}
{chr(10).join(legs)}
  </div>
  <script>
  const COR = {json.dumps(L["cor"])}, DESTAQUE = {json.dumps(L["destaque"])};
  const tl = gsap.timeline({{ paused: true }});
{chr(10).join(anim)}
  window.__timelines["main"] = tl;
  </script>
</body>
</html>
"""


def preparar_projeto_hf(pasta_hf):
    if not (pasta_hf / "hyperframes.json").exists():
        pasta_hf.parent.mkdir(parents=True, exist_ok=True)
        print(f"→ Criando projeto HyperFrames em {pasta_hf}")
        npx(["init", pasta_hf.name, "--non-interactive"], cwd=pasta_hf.parent,
            check=True, capture_output=True, text=True)
    assets = pasta_hf / "assets"
    (assets / "fontes").mkdir(parents=True, exist_ok=True)
    shutil.copy2(SKILL / "templates" / "vendor" / "gsap.min.js", assets / "gsap.min.js")
    shutil.copy2(SKILL / "templates" / "fontes" / "Montserrat.ttf",
                 assets / "fontes" / "Montserrat.ttf")


def render_final(pasta_hf, mp4, total):
    print("→ Renderizando no HyperFrames (Chrome headless + FFmpeg, CPU)...", flush=True)
    bruto = pasta_hf / "renders" / "bruto.mp4"
    bruto.parent.mkdir(exist_ok=True)
    r = npx(["render", "--output", str(bruto), "--fps", str(FPS), "--quality", "looks",
             "--no-browser-gpu"], cwd=pasta_hf)
    if r.returncode != 0 or not bruto.exists():
        sys.exit("Erro: o render do HyperFrames falhou (veja a saída acima; "
                 "`npx hyperframes doctor` ajuda a diagnosticar).")
    # garante H.264 + AAC, yuv420p, 30fps e faststart para o Instagram
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(bruto),
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         "-movflags", "+faststart", str(mp4)])
    info = json.loads(run(["ffprobe", "-v", "error", "-print_format", "json",
                           "-show_streams", "-show_format", str(mp4)], text=True).stdout)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    if v["codec_name"] != "h264" or v.get("pix_fmt") != "yuv420p":
        print("  reencodando para H.264 yuv420p...")
        tmp = Path(str(mp4) + ".tmp.mp4")
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(mp4),
             "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
             "-r", str(FPS), "-c:a", "copy", "-movflags", "+faststart", str(tmp)])
        tmp.replace(mp4)
    dur = float(info["format"]["duration"])
    print(f"✔ Reels pronto ({dur:.2f}s; esperado {total:.2f}s): {mp4}")
    print(f"  No Windows: {windows_path(Path(mp4).resolve())}")


# ------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("projeto", help="projeto.json gerado pelo cortes.py (modo reels)")
    p.add_argument("--pasta", default=None,
                   help="pasta do projeto HyperFrames (padrão: reels_<video> ao lado)")
    p.add_argument("--preview", action="store_true",
                   help="abre o Studio do HyperFrames (localhost) para aprovar")
    p.add_argument("--porta", type=int, default=3002)
    p.add_argument("--renderizar", nargs="?", const="", default=None, metavar="MP4",
                   help="renderiza o MP4 final (padrão reels_final.mp4 na pasta do vídeo)")
    args = p.parse_args()

    projeto, cam, _ = load_projeto(args.projeto)
    if projeto["modo"] != "reels":
        sys.exit("Erro: este projeto é do modo youtube; use youtube_timeline.py.")
    pasta_video = Path(args.projeto).resolve().parent
    pasta_hf = Path(args.pasta or pasta_video / f"reels_{cam['path'].stem}").resolve()

    preparar_projeto_hf(pasta_hf)
    make_base(cam, projeto, pasta_hf,
              projeto.get("enquadramento", {}).get("seguir_rosto", True))
    segs = segmentos(projeto)
    total = round(sum(z - a for a, z, _ in segs), 3)
    grupos = grupos_legenda(palavras_na_timeline(projeto, segs),
                            projeto["legenda"]["palavras_max"], total)
    (pasta_hf / "index.html").write_text(compor_html(projeto, segs, grupos, total),
                                         encoding="utf-8")
    print(f"→ Composição: {len(segs)} trechos, {len(grupos)} legendas, "
          f"{fmt_tc(total)} ({pasta_hf / 'index.html'})")
    if total > 90:
        print(f"  ⚠ {total:.0f}s passa de 90s: talvez valha cortar mais para o Reels")

    lint = npx(["lint"], cwd=pasta_hf, capture_output=True, text=True)
    saida = (lint.stdout + lint.stderr).strip()
    contagem = next((linha.strip(" ◇") for linha in saida.splitlines()
                     if "error(s)" in linha), "")
    print("→ Lint do HyperFrames: " + ("ok" if lint.returncode == 0 else "com erros")
          + (f" ({contagem})" if contagem else ""))
    # os avisos de "sub-composição" das legendas são só de organização no Studio
    if lint.returncode != 0:
        print("  " + saida.replace("\n", "\n  "))

    if args.preview:
        npx(["preview", "--background", "--port", str(args.porta)], cwd=pasta_hf,
            capture_output=True, text=True)
        print(f"\n✔ Preview: http://localhost:{args.porta}/#project/{quote(pasta_hf.name)}")
        print("  (recarrega sozinho quando o index.html muda; "
              "para encerrar: npx hyperframes preview --stop)")
    if args.renderizar is not None:
        mp4 = Path(args.renderizar or pasta_video / "reels_final.mp4").resolve()
        render_final(pasta_hf, mp4, total)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        sys.exit(f"Erro rodando {e.cmd[0]}: {e.stderr}")
