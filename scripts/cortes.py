#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["numpy", "elevenlabs"]
# ///
"""
cortes.py — base comum dos modos `youtube` e `reels`: transcrição → projeto.json.

O que faz:
  1. Transcreve a câmera pela ElevenLabs (com cache ao lado do vídeo) e gera
     transcricao.txt. Com --sem-api, usa o silencedetect do ffmpeg.
  2. Corta os silêncios pela pausa entre palavras (--silencio-min; padrão 0.7s
     no youtube e 0.35s no reels), mantendo --respiro antes/depois da fala.
  3. youtube + tela: sincroniza a tela (data de criação ou --offset) e detecta
     quando ela se mexe (ou lê --tela-csv).
  4. Aplica os takes aprovados (--takes takes.csv: inicio,fim,motivo).
  5. Grava tudo no projeto.json, a fonte única da verdade: a timeline.html,
     a composição do Reels e os renders leem dele.

Todos os tempos são no tempo ORIGINAL da câmera.

Uso:
  uv run cortes.py youtube camera.mp4 tela.mov
  uv run cortes.py reels video.mp4
  uv run cortes.py youtube camera.mp4 tela.mov --takes takes.csv --sobrescrever
  uv run cortes.py --aplicar-takes takes.csv --projeto projeto.json
      (marca takes num projeto já editado, sem perder as edições)
"""

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from fractions import Fraction
from pathlib import Path

import numpy as np

import transcrever

STANDARD_RATES = [Fraction(24000, 1001), Fraction(24), Fraction(25),
                  Fraction(30000, 1001), Fraction(30), Fraction(50),
                  Fraction(60000, 1001), Fraction(60)]

# ---------------------------------------------------------------- utilidades

def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, check=True, **kw)


def probe(path):
    out = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", str(path)], text=True)
    d = json.loads(out.stdout)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    audio = [s for s in d["streams"] if s["codec_type"] == "audio"]

    fr = Fraction(v.get("avg_frame_rate") or "0/1")
    if fr == 0:
        fr = Fraction(v.get("r_frame_rate") or "30/1")
    fr = min(STANDARD_RATES, key=lambda r: abs(float(r) - float(fr)))

    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []):
        if "rotation" in sd:
            rot = abs(int(sd["rotation"]))
    if rot in (90, 270):
        w, h = h, w

    tags = {**d["format"].get("tags", {}), **v.get("tags", {})}
    created = (tags.get("com.apple.quicktime.creationdate")
               or tags.get("creation_time"))
    return {
        "path": Path(path).resolve(),
        "duration": float(d["format"]["duration"]),
        "fps": fr,
        "width": w,
        "height": h,
        "has_audio": bool(audio),
        "audio_rate": int(audio[0].get("sample_rate", 48000)) if audio else 48000,
        "audio_channels": int(audio[0].get("channels", 2)) if audio else 2,
        "created": parse_date(created),
    }


def parse_date(s):
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)  # -0300 -> -03:00
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def fmt_tc(t):
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def parse_tc(s):
    s = s.strip()
    if ":" not in s:
        return float(s)
    parts = [float(p) for p in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def merge_intervals(iv, gap=0.0):
    iv = sorted(iv)
    out = []
    for a, b in iv:
        if out and a - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [tuple(x) for x in out]


def subtract_intervals(keep, cuts):
    """Remove de `keep` tudo que cai dentro de `cuts`."""
    out = []
    for a, b in keep:
        segs = [(a, b)]
        for s, e in cuts:
            nxt = []
            for x, y in segs:
                if e <= x or s >= y:
                    nxt.append((x, y))
                    continue
                if s > x:
                    nxt.append((x, s))
                if e < y:
                    nxt.append((e, y))
            segs = nxt
        out.extend(segs)
    return out


def windows_path(path):
    """Caminho do WSL -> C:\\... para o editor no Windows achar a mídia."""
    try:
        win = subprocess.run(["wslpath", "-w", str(path)], capture_output=True,
                             text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        print(f"  ⚠ wslpath indisponível; usando caminho Linux para {path.name}")
        return None
    if win.startswith("\\\\"):
        print(f"  ⚠ {path.name} está dentro do Linux ({win}). O editor lê melhor "
              "arquivos em /mnt/c/...; se abrir como offline, mova o vídeo.")
    return win


# ----------------------------------------------------- 1. silêncio da câmera

def voiced_parts(cam, a, b, noise_db, min_silence):
    """Trechos com som dentro de [a, b], pelo silencedetect só nessa janela."""
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}",
         "-i", str(cam["path"]), "-vn",
         "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
         "-f", "null", "-"],
        capture_output=True, text=True)
    starts = [a + float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", res.stderr)]
    ends = [a + float(x) for x in re.findall(r"silence_end: ([\d.]+)", res.stderr)]
    if len(ends) < len(starts):
        ends.append(b)
    return subtract_intervals([(a, b)], list(zip(starts, ends)))


def fix_long_words(cam, words, max_len, noise_db, min_silence):
    """A API às vezes estica uma palavra por cima de um silêncio longo.
    Nessas, mantém só a parte que tem som de verdade."""
    out = []
    for w in words:
        if w["end"] - w["start"] <= max_len:
            out.append(w)
            continue
        parts = voiced_parts(cam, w["start"], w["end"], noise_db, min_silence)
        kept = sum(y - x for x, y in parts)
        print(f"  palavra longa {w['text']!r} em {fmt_tc(w['start'])} "
              f"({w['end'] - w['start']:.1f}s) → {kept:.1f}s com som")
        out.extend({**w, "start": x, "end": y} for x, y in parts)
    return out


def detect_speech_words(cam, min_silence, padding, refazer, max_word, noise_db):
    data = transcrever.transcribe(cam["path"], refazer)
    words = transcrever.words_of(data)
    if not words:
        sys.exit("Erro: a transcrição não tem palavras. Confira o áudio ou use --sem-api.")
    print(f"→ Cortando pausas maiores que {min_silence}s entre {len(words)} palavras")
    timed = fix_long_words(cam, words, max_word, noise_db, min_silence)
    return transcrever.speech_intervals(timed, min_silence, padding,
                                        cam["duration"]), words


def detect_speech(cam, noise_db, min_silence, padding):
    print(f"→ Procurando silêncios (abaixo de {noise_db} dB por {min_silence}s)...")
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(cam["path"]), "-vn",
         "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
         "-f", "null", "-"],
        capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", res.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", res.stderr)]
    if len(ends) < len(starts):
        ends.append(cam["duration"])

    keep, cursor = [], 0.0
    for s, e in zip(starts, ends):
        if s > cursor:
            keep.append((cursor, s))
        cursor = e
    if cursor < cam["duration"]:
        keep.append((cursor, cam["duration"]))

    padded = [(max(0.0, a - padding), min(cam["duration"], b + padding))
              for a, b in keep]
    return merge_intervals(padded)


# ------------------------------------------------------ 2. sincronização

def compute_offset(cam, scr, manual):
    """Retorna delta: tempo_camera = tempo_tela + delta."""
    if manual is not None:
        print(f"→ Offset manual: a tela começou {manual:+.2f}s em relação à câmera")
        return manual
    if cam["created"] and scr["created"]:
        a, b = cam["created"], scr["created"]
        if (a.tzinfo is None) != (b.tzinfo is None):
            a, b = a.replace(tzinfo=None), b.replace(tzinfo=None)
        delta = (b - a).total_seconds()
        # Celulares (ex.: Samsung) gravam a data do FIM da gravação; o Mac grava o
        # início. Se a tela "começou" antes da câmera quase a duração inteira dela,
        # é isso: desconta a duração da câmera.
        if delta < -60 and -60 <= delta + cam["duration"] <= 900:
            delta += cam["duration"]
            print("→ A data da câmera parece ser a do fim da gravação (celular); "
                  "descontando a duração dela")
        print(f"→ Pela data dos arquivos, a tela começou {delta:+.2f}s depois da câmera")
        print("  (datas têm alguns segundos de erro: confira na timeline e ajuste o "
              "\"offset\" do projeto.json se a tela estiver adiantada/atrasada)")
        if abs(delta) > 600:
            print("  ⚠ Diferença maior que 10 min: o relógio de algum aparelho "
                  "pode estar errado. Confira e use --offset.")
        return delta
    print("  ⚠ Não achei data de criação nos arquivos. Usando offset 0; "
          "ajuste com --offset.")
    return 0.0


# ------------------------------------------- 3. quando a tela está "ativa"

def detect_screen_activity(scr, delta, threshold, gap, min_len, pad, cam_dur):
    print("→ Analisando movimento na gravação da tela (pode levar alguns minutos)...")
    W, H, FPS = 96, 54, 2
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(scr["path"]),
         "-vf", f"fps={FPS},scale={W}:{H},format=gray",
         "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE)
    raw = proc.stdout.read()
    proc.wait()
    frames = np.frombuffer(raw, np.uint8).reshape(-1, H, W).astype(np.int16)
    if len(frames) < 2:
        return []

    diff = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    diff = np.concatenate([[0.0], diff])
    n_sec = len(diff) // FPS
    per_sec = diff[: n_sec * FPS].reshape(n_sec, FPS).max(axis=1)

    active = [(i, i + 1) for i, v in enumerate(per_sec) if v > threshold]
    iv = merge_intervals(active, gap=gap)
    iv = [(a, b) for a, b in iv if b - a >= min_len]

    # tempo da tela -> tempo da câmera
    out = []
    for a, b in iv:
        ca, cb = a - pad + delta, b + pad + delta
        ca, cb = max(0.0, ca), min(cam_dur, cb)
        if cb > ca:
            out.append((ca, cb))
    return merge_intervals(out)


def save_screen_csv(path, iv):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["inicio", "fim", "obs"])
        for a, b in iv:
            w.writerow([fmt_tc(a), fmt_tc(b), ""])


def load_screen_csv(path):
    iv = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("inicio") and row.get("fim"):
                iv.append((parse_tc(row["inicio"]), parse_tc(row["fim"])))
    return merge_intervals(iv)


# ------------------------------------------------ 4. trechos mantidos

def build_pieces(keep, screen_iv, frame):
    """Divide os trechos mantidos nas fronteiras dos trechos de tela."""
    pieces = []
    for a, b in keep:
        cuts = {a, b}
        for s, e in screen_iv:
            if a < s < b:
                cuts.add(s)
            if a < e < b:
                cuts.add(e)
        pts = sorted(cuts)
        for x, y in zip(pts, pts[1:]):
            if y - x < frame:
                continue
            mid = (x + y) / 2
            is_screen = any(s <= mid < e for s, e in screen_iv)
            pieces.append((x, y, is_screen))
    return pieces




# ------------------------------------------------ 5. blocos e projeto.json

PADROES = {
    "youtube": {"silencio_min": 0.7},
    "reels": {"silencio_min": 0.35},
}

LEGENDA_PADRAO = {
    "palavras_max": 3,        # palavras por vez na tela (2–4)
    "fonte_px": 84,
    "cor": "#FFFFFF",
    "destaque": "#FFD400",    # palavra sendo falada
    "contorno": "#000000",
    "posicao_y": 0.62,        # centro da legenda, fração da altura (área segura)
    "caixa_alta": True,
}


def load_takes_csv(path):
    """CSV inicio,fim[,motivo] com os takes aprovados para remover."""
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("inicio") and row.get("fim"):
                a, b = parse_tc(row["inicio"]), parse_tc(row["fim"])
                if b > a:
                    out.append({"inicio": round(a, 3), "fim": round(b, 3),
                                "motivo": (row.get("motivo") or "").strip()})
    return sorted(out, key=lambda t: t["inicio"])


def make_blocks(keep, screen_iv, frame):
    """Trechos mantidos -> blocos, divididos nas fronteiras dos trechos de tela."""
    return [{"inicio": round(a, 3), "fim": round(b, 3), "tela": s,
             "removido": False, "motivo": ""}
            for a, b, s in build_pieces(keep, screen_iv, frame)]


def apply_takes(blocks, takes, frame, min_len):
    """Divide os blocos nas fronteiras dos takes e marca as partes como removidas
    (continuam no projeto, dá para restaurar na timeline). Sobras de fala menores
    que min_len grudadas num take também são removidas, para não piscarem."""
    out = []
    for blk in blocks:
        a, b = blk["inicio"], blk["fim"]
        pts = sorted({a, b} | {x for t in takes for x in (t["inicio"], t["fim"])
                               if a < x < b})
        for x, y in zip(pts, pts[1:]):
            if y - x < frame:
                continue
            mid = (x + y) / 2
            take = next((t for t in takes if t["inicio"] <= mid < t["fim"]), None)
            nb = {**blk, "inicio": round(x, 3), "fim": round(y, 3)}
            if take:
                nb.update(removido=True, motivo=take["motivo"] or "take repetido")
            out.append(nb)
    for i, blk in enumerate(out):
        if blk["removido"] or blk["fim"] - blk["inicio"] >= min_len:
            continue
        for j in (i - 1, i + 1):
            if 0 <= j < len(out) and out[j]["removido"] and (
                    abs(out[j]["fim"] - blk["inicio"]) < 1e-3
                    or abs(blk["fim"] - out[j]["inicio"]) < 1e-3):
                blk.update(removido=True, motivo=out[j]["motivo"])
                break
    return out


def media_json(m):
    if not m:
        return None
    return {"arquivo": str(m["path"]), "windows": windows_path(m["path"]),
            "duracao": m["duration"], "fps": f"{m['fps'].numerator}/{m['fps'].denominator}",
            "largura": m["width"], "altura": m["height"], "audio": m["has_audio"],
            "audio_rate": m["audio_rate"], "audio_canais": m["audio_channels"],
            "criado": m["created"].isoformat() if m["created"] else None}


def media_from_json(j):
    """O inverso de media_json: o dict que o resto dos scripts usa."""
    if not j:
        return None
    return {"path": Path(j["arquivo"]), "duration": j["duracao"],
            "fps": Fraction(j["fps"]), "width": j["largura"], "height": j["altura"],
            "has_audio": j["audio"], "audio_rate": j["audio_rate"],
            "audio_channels": j["audio_canais"], "created": parse_date(j["criado"])}


def load_projeto(path):
    p = json.loads(Path(path).read_text(encoding="utf-8"))
    return p, media_from_json(p["camera"]), media_from_json(p.get("tela"))


def save_projeto(path, projeto):
    projeto["blocos"].sort(key=lambda b: b["inicio"])
    Path(path).write_text(json.dumps(projeto, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def resumo(projeto):
    ativos = [b for b in projeto["blocos"] if not b["removido"]]
    final = sum(b["fim"] - b["inicio"] for b in ativos)
    takes = sum(b["fim"] - b["inicio"] for b in projeto["blocos"] if b["removido"])
    dur = projeto["camera"]["duracao"]
    print(f"  Duração original: {fmt_tc(dur)}")
    print(f"  Duração final:    {fmt_tc(final)}  (silêncios −{fmt_tc(dur - final - takes)}, "
          f"takes −{fmt_tc(takes)})")
    print(f"  Blocos:           {len(ativos)} ativos, "
          f"{len(projeto['blocos']) - len(ativos)} removidos, "
          f"{sum(b['tela'] for b in ativos)} com tela")


# ------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("modo", nargs="?", choices=["youtube", "reels"])
    p.add_argument("camera", nargs="?")
    p.add_argument("tela", nargs="?", default=None,
                   help="só youtube: gravação da tela (opcional)")
    p.add_argument("--saida", default=None,
                   help="projeto.json (padrão: projeto.json na pasta do vídeo)")
    p.add_argument("--sobrescrever", action="store_true",
                   help="refaz um projeto.json existente (guarda o antigo em .bak)")
    p.add_argument("--takes", default=None,
                   help="CSV inicio,fim,motivo com os takes aprovados")
    p.add_argument("--aplicar-takes", default=None, metavar="CSV",
                   help="marca takes num projeto existente (--projeto) sem refazê-lo")
    p.add_argument("--projeto", default=None, help="projeto.json para --aplicar-takes")
    # silêncio
    p.add_argument("--silencio-min", type=float, default=None,
                   help="pausa entre palavras que vira corte (padrão 0.7 youtube, 0.35 reels)")
    p.add_argument("--respiro", type=float, default=0.15,
                   help="margem mantida antes/depois da fala, em s (padrão 0.15)")
    p.add_argument("--sem-api", action="store_true",
                   help="não usa a ElevenLabs; corta silêncio com o silencedetect")
    p.add_argument("--ruido", type=float, default=-35,
                   help="dB abaixo do qual é silêncio (--sem-api e palavras longas)")
    p.add_argument("--palavra-max", type=float, default=2.0,
                   help="palavra mais longa que N s é conferida com silencedetect")
    p.add_argument("--clipe-min", type=float, default=0.25,
                   help="sobras menores que N s na borda de um take são removidas")
    p.add_argument("--refazer-transcricao", action="store_true",
                   help="ignora o JSON em cache e chama a API de novo")
    # sincronização e tela (youtube)
    p.add_argument("--offset", type=float, default=None,
                   help="segundos que a tela começou DEPOIS da câmera")
    p.add_argument("--tela-csv", default=None,
                   help="usa este CSV de trechos de tela em vez de detectar")
    p.add_argument("--sensibilidade", type=float, default=0.8)
    p.add_argument("--juntar", type=float, default=10)
    p.add_argument("--tela-min", type=float, default=4)
    p.add_argument("--folga-tela", type=float, default=1.5)
    args = p.parse_args()

    if args.aplicar_takes:
        if not args.projeto:
            p.error("--aplicar-takes precisa de --projeto projeto.json")
        projeto, cam, _ = load_projeto(args.projeto)
        takes = load_takes_csv(args.aplicar_takes)
        projeto["blocos"] = apply_takes(projeto["blocos"], takes,
                                        float(1 / cam["fps"]), args.clipe_min)
        projeto["takes"] = sorted(projeto.get("takes", []) + takes,
                                  key=lambda t: t["inicio"])
        save_projeto(args.projeto, projeto)
        print(f"✔ {len(takes)} takes aplicados em {args.projeto}")
        resumo(projeto)
        return

    if not (args.modo and args.camera):
        p.error("informe o modo (youtube ou reels) e o vídeo da câmera")
    if args.modo == "reels" and args.tela:
        p.error("o modo reels usa só o vídeo da câmera")
    silencio_min = args.silencio_min or PADROES[args.modo]["silencio_min"]

    cam = probe(args.camera)
    scr = probe(args.tela) if args.tela else None
    saida = Path(args.saida or cam["path"].with_name("projeto.json")).resolve()
    if saida.exists() and not args.sobrescrever:
        sys.exit(f"Erro: {saida} já existe (pode ter edições da timeline). Use "
                 "--aplicar-takes para só marcar takes, ou --sobrescrever para refazer.")
    print(f"Câmera: {fmt_tc(cam['duration'])} @ {float(cam['fps']):.2f} fps, "
          f"{cam['width']}x{cam['height']}")
    if scr:
        print(f"Tela:   {fmt_tc(scr['duration'])}")

    if args.sem_api:
        keep = detect_speech(cam, args.ruido, silencio_min, args.respiro)
        cache = None
    else:
        keep, words = detect_speech_words(cam, silencio_min, args.respiro,
                                          args.refazer_transcricao,
                                          args.palavra_max, args.ruido)
        cache = transcrever.cache_path(cam["path"])
        txt = saida.parent / "transcricao.txt"
        n = transcrever.write_txt(txt, words)
        print(f"→ Transcrição legível ({n} frases) em {txt}")

    delta, screen_iv = 0.0, []
    if scr:
        delta = compute_offset(cam, scr, args.offset)
        if args.tela_csv:
            screen_iv = load_screen_csv(args.tela_csv)
            print(f"→ Usando {len(screen_iv)} trechos de tela de {args.tela_csv}")
        else:
            screen_iv = detect_screen_activity(scr, delta, args.sensibilidade,
                                               args.juntar, args.tela_min,
                                               args.folga_tela, cam["duration"])
            csv_path = saida.parent / "trechos_tela.csv"
            save_screen_csv(csv_path, screen_iv)
            print(f"→ {len(screen_iv)} trechos de tela detectados "
                  f"(cópia editável em {csv_path})")

    frame = float(1 / cam["fps"])
    blocks = make_blocks(keep, screen_iv, frame)
    takes = load_takes_csv(args.takes) if args.takes else []
    if takes:
        blocks = apply_takes(blocks, takes, frame, args.clipe_min)
        print(f"→ {len(takes)} takes marcados como removidos")

    projeto = {
        "versao": 1,
        "modo": args.modo,
        "criado_em": datetime.now().isoformat(timespec="seconds"),
        "camera": media_json(cam),
        "tela": media_json(scr),
        "transcricao": str(cache) if cache else None,
        "offset": round(delta, 3),
        "parametros": {"silencio_min": silencio_min, "respiro": args.respiro,
                       "sem_api": args.sem_api},
        "trechos_tela": [[round(a, 3), round(b, 3)] for a, b in screen_iv],
        "takes": takes,
        "blocos": blocks,
    }
    if args.modo == "youtube":
        projeto["formato"] = {"largura": 1920, "altura": 1080,
                              "fps": projeto["camera"]["fps"]}
        projeto["layout"] = {"escala_tela": 0.72, "escala_camera": 0.25,
                             "margem": 24, "cor_fundo": "black"}
        projeto["borroes"] = []          # preenchido pelo sigilo.py
        projeto["redes"] = None          # {"handle": "@...", "plataformas": [...]}
        projeto["sobreposicoes"] = []    # [{"tipo": "redes", "inicio": s, "fim": s}]
    else:
        projeto["formato"] = {"largura": 1080, "altura": 1920, "fps": "30/1"}
        projeto["legenda"] = dict(LEGENDA_PADRAO)
        projeto["gancho"] = None
        projeto["enquadramento"] = {"seguir_rosto": cam["width"] > cam["height"]}

    if saida.exists():
        shutil.copy2(saida, saida.with_suffix(".json.bak"))
    save_projeto(saida, projeto)
    print(f"\n✔ Projeto salvo em {saida}")
    resumo(projeto)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        sys.exit(f"Erro rodando {e.cmd[0]}: {e.stderr}")
