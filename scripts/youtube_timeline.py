#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["numpy", "elevenlabs", "opentimelineio"]
# ///
"""
youtube_timeline.py — modo youtube: projeto.json → proxies + timeline.html + render.

O que faz (sempre a partir do projeto.json gerado pelo cortes.py):
  1. Gera proxies leves (480p, H.264, CPU) da câmera e da tela em proxies/.
  2. Gera timeline.html (arquivo único, abre direto no Chrome): barra do tempo
     com fala, silêncios cortados, trechos de tela e takes removidos; preview
     com o layout; transcrição clicável; arrastar bordas, ligar/desligar tela,
     remover/restaurar, desfazer; botão Salvar.
  3. --servir: sobe um servidor local (com Range, para o vídeo avançar) em que
     o Salvar grava direto no projeto.json.
  4. --renderizar: monta o MP4 final no ffmpeg com os originais (libx264, CPU),
     trecho a trecho, e junta tudo no fim.
  5. --otio: exporta timeline.otio para o Kdenlive 25.04+.

Uso:
  uv run youtube_timeline.py projeto.json                 # proxies + timeline.html
  uv run youtube_timeline.py projeto.json --servir        # http://localhost:8765/timeline.html
  uv run youtube_timeline.py projeto.json --renderizar    # video_final.mp4
  uv run youtube_timeline.py projeto.json --otio
"""

import argparse
import json
import subprocess
import sys
import time
from fractions import Fraction
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import transcrever
from cortes import fmt_tc, load_projeto, run, save_projeto, windows_path

TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "timeline.html"


# ------------------------------------------- 5. render e OTIO (Kdenlive)

def edit_list(cam, scr, pieces, delta):
    """Trechos em frames da câmera: (frame_inicial, n_frames, inicio_na_tela|None).
    É a mesma lista para o OTIO e para o render."""
    fd = 1 / cam["fps"]

    def frames_of(seconds):
        return round(Fraction(seconds).limit_denominator(100000) / fd)

    segs = []
    for a, b, is_screen in pieces:
        fa = frames_of(a)
        n = frames_of(b) - fa
        if n <= 0:
            continue
        scr_start = None
        if is_screen and scr:
            s = float(fa * fd) - delta
            if 0 <= s and s + float(n * fd) <= scr["duration"] + 0.5:
                scr_start = s
        segs.append((fa, n, scr_start))
    return segs


def write_otio(out, cam, scr, segs, args):
    """V1 câmera, V2 tela (só nos trechos de tela), A1 áudio da câmera.
    O Kdenlive lê target_url como caminho local, então vai C:\\... e não file://."""
    import opentimelineio as otio

    rate = float(cam["fps"])
    fd = 1 / cam["fps"]

    def tr(start, dur):
        return otio.opentime.TimeRange(otio.opentime.RationalTime(start, rate),
                                       otio.opentime.RationalTime(dur, rate))

    def ref(media):
        total = round(Fraction(media["duration"]).limit_denominator(100000) / fd)
        return otio.schema.ExternalReference(
            target_url=windows_path(media["path"]) or str(media["path"]),
            available_range=tr(0, total))

    v1 = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    v2 = otio.schema.Track(name="V2", kind=otio.schema.TrackKind.Video)
    a1 = otio.schema.Track(name="A1", kind=otio.schema.TrackKind.Audio)
    gap = 0
    for fa, n, scr_start in segs:
        v1.append(otio.schema.Clip(name="camera", media_reference=ref(cam),
                                   source_range=tr(fa, n)))
        a1.append(otio.schema.Clip(name="camera", media_reference=ref(cam),
                                   source_range=tr(fa, n)))
        if scr_start is None:
            gap += n
            continue
        if gap:
            v2.append(otio.schema.Gap(source_range=tr(0, gap)))
            gap = 0
        sf = round(Fraction(scr_start).limit_denominator(100000) / fd)
        v2.append(otio.schema.Clip(name="tela", media_reference=ref(scr),
                                   source_range=tr(sf, n)))

    tl = otio.schema.Timeline(name=args.nome)
    tl.tracks.append(v1)            # 1ª trilha de vídeo = a de baixo
    if len(v2):
        tl.tracks.append(v2)
    tl.tracks.append(a1)
    otio.adapters.write_to_file(tl, str(out))


def even(x):
    return max(2, int(round(x / 2)) * 2)


def blur_chain(entrada, caixas, scr):
    """Borra cada caixa (x,y,w,h em px da tela original) entre os tempos a e b
    (em segundos do trecho). Devolve (filtros, rótulo de saída)."""
    partes, cur = [], entrada
    for k, (a, b, x, y, w, h) in enumerate(caixas):
        x, y = min(max(0, int(x)) // 2 * 2, scr["width"] - 4), min(max(0, int(y)) // 2 * 2, scr["height"] - 4)
        w = max(4, min(int(w), scr["width"] - x) // 2 * 2)
        h = max(4, min(int(h), scr["height"] - y) // 2 * 2)
        partes.append(f"{cur}split[bz{k}a][bz{k}b];"
                      f"[bz{k}b]crop={w}:{h}:{x}:{y},"
                      "boxblur=luma_radius='min(w,h)/3':luma_power=3:"
                      f"chroma_radius='min(cw,ch)/3':chroma_power=3[bz{k}c];"
                      f"[bz{k}a][bz{k}c]overlay={x}:{y}:enable='between(t,{a:.3f},{b:.3f})'[bz{k}]")
        cur = f"[bz{k}]"
    return partes, cur


def card_overlay(entrada, idx, card, F0, dur, W, H):
    """Card das redes no canto inferior esquerdo, deslizando da esquerda.
    As janelas do card estão no tempo do vídeo FINAL; F0 é o início do trecho."""
    m, ent = 48, 0.45
    janelas = [(a, b) for a, b in card["janelas"] if a < F0 + dur and b > F0]
    if not janelas:
        return [], entrada
    partes, cur = [f"[{idx}:v]format=rgba[card]"], entrada
    for k, (a, b) in enumerate(janelas):
        T = f"(t+{F0:.4f})"
        p = f"clip(min(({T}-{a:.3f})/{ent},({b:.3f}-{T})/{ent}),0,1)"
        x = f"{m}-({m}+overlay_w)*pow(1-{p},3)"
        rot = "[card]" if len(janelas) == 1 else f"[card{k}]"
        if len(janelas) > 1 and k == 0:
            partes[0] = "[%d:v]format=rgba,split=%d%s" % (
                idx, len(janelas), "".join(f"[card{j}]" for j in range(len(janelas))))
        partes.append(f"{cur}{rot}overlay=x='{x}':y={H - m - card['h']}:"
                      f"enable='between(t+{F0:.4f},{a:.3f},{b:.3f})'[cd{k}]")
        cur = f"[cd{k}]"
    return partes, cur


def render(out, cam, scr, segs, args, borroes=(), card=None):
    """Renderiza cada trecho num arquivo (cache) e junta tudo com o concat.
    borroes: (ini, fim, x, y, w, h) no tempo da tela; card: PNG das redes."""
    import hashlib
    import time

    fd = 1 / cam["fps"]
    fps = f"{cam['fps'].numerator}/{cam['fps'].denominator}"
    W, H, m = even(args.largura), even(args.altura), int(args.margem)
    sw, sh = even(W * args.escala_tela), even(H * args.escala_tela)
    cw, ch = even(W * args.escala_camera), even(H * args.escala_camera)
    fit = "force_original_aspect_ratio=decrease:force_divisible_by=2"

    def filtro(fa, n, scr_start, F0):
        dur = float(n * fd)
        if scr_start is None:
            partes = [f"[0:v]fps={fps},scale={W}:{H}:{fit},"
                      f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={args.cor_fundo},setsar=1[base]"]
            prox = 1
        else:
            caixas = [(max(0.0, a - scr_start), min(dur, b - scr_start), x, y, w, h)
                      for a, b, x, y, w, h in borroes
                      if a < scr_start + dur and b > scr_start]
            partes, s = blur_chain("[1:v]", caixas, scr)
            partes += [f"color=c={args.cor_fundo}:s={W}x{H}:r={fps}[bg]",
                       f"{s}fps={fps},scale={sw}:{sh}:{fit},setsar=1[s]",
                       f"[0:v]fps={fps},scale={cw}:{ch}:{fit},setsar=1[c]",
                       f"[bg][s]overlay={m}:{m}:shortest=1[t]",
                       f"[t][c]overlay=main_w-overlay_w-{m}:main_h-overlay_h-{m}[base]"]
            prox = 2
        cur, usa_card = "[base]", False
        if card:
            extra, cur = card_overlay(cur, prox, card, F0, dur, W, H)
            partes += extra
            usa_card = bool(extra)
        partes.append(f"{cur}format=yuv420p[v]")
        return ";".join(partes), usa_card

    cache = Path(out).resolve().parent / "render_cache"
    cache.mkdir(exist_ok=True)
    files, t0, novos, F0 = [], time.time(), 0, 0.0
    print(f"→ Renderizando {len(segs)} trechos (libx264 {args.preset}, crf {args.crf}); "
          f"cache em {cache}")
    for i, (fa, n, scr_start) in enumerate(segs, 1):
        graph, usa_card = filtro(fa, n, scr_start, F0)
        F0 += float(n * fd)
        key = json.dumps([str(cam["path"]), fa, n, str(scr and scr["path"]),
                          scr_start and round(scr_start, 4), graph,
                          usa_card and card["hash"], args.crf, args.preset])
        seg = cache / (hashlib.sha1(key.encode()).hexdigest()[:20] + ".mkv")
        files.append(seg)
        if seg.exists():
            continue
        start, dur = float(fa * fd), float(n * fd)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-ss", f"{start:.6f}", "-i", str(cam["path"])]
        if scr_start is not None:
            cmd += ["-ss", f"{scr_start:.6f}", "-i", str(scr["path"])]
        if usa_card:
            cmd += ["-loop", "1", "-framerate", fps, "-i", str(card["png"])]
        audio_in = "0:a:0"
        if not cam["has_audio"]:
            audio_in = f"{1 + (scr_start is not None) + usa_card}:a"
            cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        tmp = seg.with_suffix(".tmp.mkv")
        cmd += ["-filter_complex", graph,
                "-map", "[v]", "-map", audio_in, "-af", "apad",
                "-frames:v", str(n), "-t", f"{dur:.6f}",
                "-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
                "-r", fps, "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
                str(tmp)]
        run(cmd)
        tmp.rename(seg)
        novos += 1
        el = time.time() - t0
        print(f"  [{i}/{len(segs)}] {fmt_tc(start)} ({dur:.1f}s)  "
              f"decorrido {fmt_tc(el)[:8]}, faltam ~{fmt_tc(el / i * (len(segs) - i))[:8]}",
              flush=True)

    for old in cache.glob("*.mkv"):          # trechos de versões antigas
        if old not in files:
            old.unlink()
    lista = cache / "lista.txt"
    lista.write_text("".join(f"file '{f.name}'\n" for f in files), encoding="utf-8")
    print(f"→ {novos} trechos novos, {len(files) - novos} reaproveitados. Juntando...")
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "concat", "-safe", "0", "-i", str(lista),
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-movflags", "+faststart", str(out)])




# --------------------------------------- 6. card das redes e borrões

ICONES = Path(__file__).resolve().parent.parent / "templates" / "icones"
FONTE = Path(__file__).resolve().parent.parent / "templates" / "fontes" / "Montserrat.ttf"
CORES = {"instagram": "#E4405F", "tiktok": "#000000", "youtube": "#FF0000",
         "linkedin": "#0A66C2", "x": "#000000", "github": "#181717"}


def tempo_final(projeto, t):
    """Tempo da câmera -> tempo no vídeo final (soma dos blocos ativos antes de t)."""
    s = 0.0
    for b in projeto["blocos"]:
        if b["removido"]:
            continue
        if b["fim"] <= t:
            s += b["fim"] - b["inicio"]
        elif b["inicio"] <= t:
            s += t - b["inicio"]
    return s


def borroes_ativos(projeto):
    return [(o["ini"], o["fim"], o["x"], o["y"], o["w"], o["h"])
            for b in projeto.get("borroes", []) if b.get("ativo", True)
            for o in b["ocorrencias"]]


def chrome_path():
    achados = sorted((Path.home() / ".cache" / "hyperframes" / "chrome").glob(
        "**/chrome-headless-shell"))
    return achados[-1] if achados else None


def gerar_card(projeto, pasta):
    """PNG transparente do card das redes (Chrome headless). None se não houver."""
    import hashlib

    redes = projeto.get("redes")
    sob = [s for s in projeto.get("sobreposicoes", []) if s.get("tipo") == "redes"]
    if not redes or not sob:
        return None
    chrome = chrome_path()
    if not chrome:
        print("  ⚠ Chrome headless não encontrado (npx hyperframes browser ensure); "
              "vídeo sai sem o card das redes")
        return None
    icones = "".join(
        f'<span class="ic" style="background:{CORES.get(n, "#333")}">'
        f'{(ICONES / f"{n}.svg").read_text()}</span>'
        for n in redes.get("plataformas", []) if (ICONES / f"{n}.svg").exists())
    html_card = f"""<!doctype html><html><head><meta charset="utf-8"><style>
@font-face {{ font-family: C; src: url("{FONTE.as_uri()}"); font-weight: 100 900; }}
html, body {{ margin: 0; background: transparent; }}
.card {{ display: inline-flex; align-items: center; gap: 22px; padding: 20px 36px 20px 20px;
  background: rgba(14, 14, 18, .88); border-radius: 26px; font-family: C, sans-serif;
  border-left: 8px solid {redes.get("cor", "#FFD400")}; }}
.ics {{ display: flex; gap: 12px; }}
.ic {{ width: 76px; height: 76px; border-radius: 50%; display: grid; place-items: center;
  box-shadow: inset 0 0 0 2px rgba(255,255,255,.18); }}
.ic svg {{ width: 42px; height: 42px; fill: #fff; }}
.sub {{ color: #b9bec7; font-size: 24px; font-weight: 700; letter-spacing: .08em;
  text-transform: uppercase; }}
.handle {{ color: #fff; font-size: 50px; font-weight: 800; line-height: 1.05; }}
</style></head><body><div class="card"><div class="ics">{icones}</div>
<div><div class="sub">{redes.get("chamada", "me segue nas redes")}</div>
<div class="handle">{redes["handle"]}</div></div></div></body></html>"""
    assets = pasta / "assets"
    assets.mkdir(exist_ok=True)
    fonte_html, bruto, png = assets / "redes.html", assets / "redes_bruto.png", assets / "redes.png"
    fonte_html.write_text(html_card, encoding="utf-8")
    run([str(chrome), "--headless", "--no-sandbox", "--hide-scrollbars",
         "--default-background-color=00000000", "--window-size=1600,240",
         f"--screenshot={bruto}", fonte_html.as_uri()])
    # recorta o transparente em volta
    info = json.loads(run(["ffprobe", "-v", "error", "-print_format", "json",
                           "-show_streams", str(bruto)], text=True).stdout)["streams"][0]
    w, h = info["width"], info["height"]
    raw = run(["ffmpeg", "-v", "error", "-i", str(bruto), "-f", "rawvideo",
               "-pix_fmt", "rgba", "-"]).stdout
    import numpy as np
    alfa = np.frombuffer(raw, np.uint8).reshape(h, w, 4)[:, :, 3]
    ys, xs = np.nonzero(alfa > 8)
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    cw, ch = (x1 - x0) // 2 * 2, (y1 - y0) // 2 * 2
    run(["ffmpeg", "-v", "error", "-y", "-i", str(bruto), "-vf",
         f"crop={cw}:{ch}:{x0}:{y0}", str(png)])
    bruto.unlink()
    janelas = [(round(tempo_final(projeto, s["inicio"]), 3),
                round(tempo_final(projeto, s["fim"]), 3)) for s in sob]
    return {"png": png, "w": int(cw), "h": int(ch), "janelas": janelas,
            "hash": hashlib.md5(png.read_bytes()).hexdigest()}


# ------------------------------------------- 7. proxies e timeline.html

def make_proxy(src, dest, audio):
    """480p H.264 leve, com keyframe a cada 1s para o navegador pular rápido."""
    if dest.exists():
        print(f"→ Proxy já existe: {dest.name}")
        return
    dest.parent.mkdir(exist_ok=True)
    tmp = dest.with_suffix(".tmp.mp4")
    print(f"→ Gerando proxy {dest.name} (CPU; vídeo longo leva alguns minutos)...",
          flush=True)
    t0 = time.time()
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
           "-vf", "scale=-2:480", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "28", "-g", "30", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "96k", "-ac", "1"] if audio else ["-an"]
    run(cmd + ["-movflags", "+faststart", str(tmp)])
    tmp.rename(dest)
    print(f"  pronto em {fmt_tc(time.time() - t0)[:8]}")


def frases_do_projeto(projeto):
    if not projeto.get("transcricao") or not Path(projeto["transcricao"]).exists():
        return []
    data = json.loads(Path(projeto["transcricao"]).read_text(encoding="utf-8"))
    return [[round(a, 3), round(b, 3), t]
            for a, b, t in transcrever.phrases(transcrever.words_of(data))]


def write_html(projeto_path, projeto, proxies, card=None):
    dados = {"projeto": projeto, "frases": frases_do_projeto(projeto),
             "proxies": proxies,
             "card": card and {"src": f"assets/redes.png?{card['hash'][:8]}",
                               "w": card["w"], "h": card["h"]}}
    blob = json.dumps(dados, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("__DADOS__", blob)
    out = Path(projeto_path).resolve().parent / "timeline.html"
    out.write_text(html, encoding="utf-8")
    return out


# --------------------------------------------- 8. servidor local (Salvar)

def mesclar(disco, novo):
    """O Salvar da página só altera o que ela edita: blocos e o liga/desliga dos
    borrões. O resto (offset, borrões novos do sigilo.py, redes...) vem do disco,
    para uma aba aberta há tempo não apagar o que foi gerado depois."""
    disco["blocos"] = novo["blocos"]
    ativo = {b.get("id"): b.get("ativo", True) for b in novo.get("borroes") or []}
    for b in disco.get("borroes", []):
        if b.get("id") in ativo:
            b["ativo"] = bool(ativo[b["id"]])
    return disco

def valid_blocks(blocos):
    if not isinstance(blocos, list) or not blocos:
        return False
    for b in blocos:
        if not (isinstance(b, dict) and isinstance(b.get("inicio"), (int, float))
                and isinstance(b.get("fim"), (int, float)) and b["fim"] > b["inicio"]
                and isinstance(b.get("tela"), bool) and isinstance(b.get("removido"), bool)):
            return False
    return True


def serve(projeto_path, host, port):
    projeto_path = Path(projeto_path).resolve()
    root = projeto_path.parent

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(root), **kw)

        def log_message(self, *a):
            pass

        def end_headers(self):
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/projeto.json":
                body = projeto_path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            rng = self.headers.get("Range")
            f = Path(self.translate_path(self.path))
            if not rng or not f.is_file():
                return super().do_GET()
            # Range: o <video> do navegador só consegue pular de trecho com isso
            size = f.stat().st_size
            a, _, b = rng.replace("bytes=", "").partition("-")
            start = int(a) if a else max(0, size - int(b))
            end = int(b) if a and b else size - 1
            end = min(end, size - 1)
            if start > end:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                return
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Type", self.guess_type(str(f)))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            try:
                with open(f, "rb") as fh:
                    fh.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = fh.read(min(1 << 20, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass                    # o navegador cancela pedidos ao pular

        def do_POST(self):
            if self.path.split("?")[0] != "/salvar":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            try:
                novo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if not valid_blocks(novo.get("blocos")):
                    raise ValueError("blocos inválidos")
            except Exception as e:  # noqa: BLE001 — resposta para o navegador
                self.send_error(HTTPStatus.BAD_REQUEST, str(e))
                return
            atual = projeto_path.read_bytes()
            projeto_path.with_suffix(".json.bak").write_bytes(atual)
            novo = mesclar(json.loads(atual), novo)
            save_projeto(projeto_path, novo)
            print(f"  salvo: {time.strftime('%H:%M:%S')} "
                  f"({sum(not b['removido'] for b in novo['blocos'])} blocos ativos)",
                  flush=True)
            body = b'{"ok": true}'
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"\n✔ Timeline em http://localhost:{port}/timeline.html")
    print("  (o Salvar grava direto no projeto.json; Ctrl+C encerra)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("projeto", help="projeto.json gerado pelo cortes.py (modo youtube)")
    p.add_argument("--servir", action="store_true",
                   help="sobe um servidor local para a timeline salvar direto")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--porta", type=int, default=8765)
    p.add_argument("--renderizar", nargs="?", const="", default=None, metavar="MP4",
                   help="renderiza o vídeo final (padrão video_final.mp4)")
    p.add_argument("--crf", type=int, default=20)
    p.add_argument("--preset", default="medium")
    p.add_argument("--otio", nargs="?", const="", default=None, metavar="OTIO",
                   help="exporta timeline.otio para o Kdenlive")
    args = p.parse_args()

    projeto, cam, scr = load_projeto(args.projeto)
    if projeto["modo"] != "youtube":
        sys.exit("Erro: este projeto é do modo reels; use reels_compor.py.")
    pasta = Path(args.projeto).resolve().parent
    lay, fmt = projeto["layout"], projeto["formato"]
    opts = SimpleNamespace(nome=pasta.name, largura=fmt["largura"],
                           altura=fmt["altura"], margem=lay["margem"],
                           escala_tela=lay["escala_tela"],
                           escala_camera=lay["escala_camera"],
                           cor_fundo=lay["cor_fundo"], crf=args.crf, preset=args.preset)
    ativos = [(b["inicio"], b["fim"], b["tela"])
              for b in projeto["blocos"] if not b["removido"]]
    segs = edit_list(cam, scr, ativos, projeto["offset"])

    card = gerar_card(projeto, pasta)
    if args.renderizar is not None:
        mp4 = args.renderizar or str(pasta / "video_final.mp4")
        borroes = borroes_ativos(projeto)
        if borroes:
            print(f"→ {len(borroes)} borrões de dados sigilosos na tela")
        if card:
            print(f"→ Card das redes em {len(card['janelas'])} momento(s)")
        render(mp4, cam, scr, segs, opts, borroes, card)
        final = float(sum(n for _, n, _ in segs) / cam["fps"])
        print(f"\n✔ Vídeo final ({fmt_tc(final)}): {mp4}")
        print(f"  No Windows: {windows_path(Path(mp4).resolve())}")
        return
    if args.otio is not None:
        out = args.otio or str(pasta / "timeline.otio")
        write_otio(out, cam, scr, segs, opts)
        print(f"✔ OTIO para o Kdenlive: {out}")
        return

    proxies = {"camera": "proxies/camera_480p.mp4", "tela": None}
    make_proxy(cam["path"], pasta / proxies["camera"], True)
    if scr:
        proxies["tela"] = "proxies/tela_480p.mp4"
        make_proxy(scr["path"], pasta / proxies["tela"], False)
    html = write_html(args.projeto, projeto, proxies, card)
    print(f"✔ Timeline: {html}")
    print(f"  No Chrome: {windows_path(html)}")
    if args.servir:
        serve(args.projeto, args.host, args.porta)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        sys.exit(f"Erro rodando {e.cmd[0]}: {e.stderr}")
