#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["elevenlabs"]
# ///
"""
transcrever.py — transcreve o áudio da câmera pela API da ElevenLabs (Scribe).

O que faz:
  1. Extrai só o áudio da câmera (mono, 64 kbps) com ffmpeg.
  2. Manda para a ElevenLabs (scribe_v2, timestamps por palavra, português).
  3. Salva o JSON bruto ao lado do vídeo (<video>.transcricao.json). Se esse
     arquivo já existir, ele é reaproveitado e a API NÃO é chamada de novo.
  4. Gera uma transcrição legível (transcricao.txt) com timecode por frase.

A chave vem de ELEVENLABS_API_KEY (variável de ambiente).

Uso:
  uv run transcrever.py camera.mp4
  uv run transcrever.py camera.mp4 --txt pasta/transcricao.txt
  uv run transcrever.py camera.mp4 --refazer      # ignora o cache e paga de novo
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

FIM_DE_FRASE = (".", "?", "!", "…")


def fmt_tc(t):
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def cache_path(video):
    video = Path(video).resolve()
    return video.with_name(video.stem + ".transcricao.json")


# ------------------------------------------------------------- API

def extract_audio(video, dest):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(video), "-vn", "-ac", "1", "-b:a", "64k",
                    str(dest)], check=True, capture_output=True)


def api_key():
    """ELEVENLABS_API_KEY do ambiente ou do .env na pasta da skill."""
    key = os.environ.get("ELEVENLABS_API_KEY")
    env = Path(__file__).resolve().parent.parent / ".env"
    if not key and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and name.strip() == "ELEVENLABS_API_KEY":
                key = value.strip().strip("'\"")
    return key


def call_elevenlabs(audio_path):
    key = api_key()
    if not key:
        sys.exit("Erro: defina ELEVENLABS_API_KEY (no ambiente ou no .env da skill) "
                 "ou rode com --sem-api para usar só o silencedetect.")
    from elevenlabs.client import ElevenLabs

    client = ElevenLabs(api_key=key)
    with open(audio_path, "rb") as f:
        resp = client.speech_to_text.convert(
            file=f,
            model_id="scribe_v2",
            timestamps_granularity="word",
            language_code="por",
            request_options={"timeout_in_seconds": 3600},
        )
    if hasattr(resp, "model_dump"):
        return resp.model_dump(mode="json")
    if hasattr(resp, "dict"):
        return resp.dict()
    return json.loads(resp.json())


def transcribe(video, refazer=False):
    """Devolve o JSON da transcrição, usando o cache ao lado do vídeo se existir."""
    cache = cache_path(video)
    if cache.exists() and not refazer:
        print(f"→ Usando transcrição em cache: {cache}")
        return json.loads(cache.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / "audio.mp3"
        print("→ Extraindo áudio da câmera...")
        extract_audio(video, audio)
        mb = audio.stat().st_size / 1e6
        print(f"→ Enviando {mb:.1f} MB para a ElevenLabs (scribe_v2)... "
              "pode levar alguns minutos")
        data = call_elevenlabs(audio)

    cache.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ Transcrição salva em {cache}")
    return data


# ----------------------------------------------------------- leitura

def words_of(data):
    """Só as palavras faladas (sem espaços nem eventos de áudio), em ordem."""
    out = []
    for w in data.get("words") or []:
        if w.get("type", "word") != "word":
            continue
        if w.get("start") is None or w.get("end") is None:
            continue
        out.append({"text": w["text"], "start": float(w["start"]),
                    "end": float(w["end"])})
    out.sort(key=lambda w: w["start"])
    return out


def speech_intervals(words, min_silence, padding, duration):
    """Intervalos de fala: pausa entre palavras > min_silence vira corte."""
    blocks = []
    for w in words:
        if blocks and w["start"] - blocks[-1][1] <= min_silence:
            blocks[-1][1] = max(blocks[-1][1], w["end"])
        else:
            blocks.append([w["start"], w["end"]])
    out = []
    for a, b in blocks:
        a, b = max(0.0, a - padding), min(duration, b + padding)
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        elif b > a:
            out.append((a, b))
    return out


def phrases(words, max_gap=0.7):
    """Agrupa palavras em frases: quebra em pontuação final ou pausa longa."""
    out, cur = [], []
    for w in words:
        if cur and w["start"] - cur[-1]["end"] > max_gap:
            out.append(cur)
            cur = []
        cur.append(w)
        if w["text"].rstrip().endswith(FIM_DE_FRASE):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [(p[0]["start"], p[-1]["end"],
             " ".join(w["text"].strip() for w in p)) for p in out]


def write_txt(path, words):
    lines = [f"[{fmt_tc(a)} → {fmt_tc(b)}] {txt}" for a, b, txt in phrases(words)]
    Path(path).write_text(
        "# Timecodes no tempo ORIGINAL da câmera (antes dos cortes)\n"
        + "\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("camera")
    p.add_argument("--txt", default=None,
                   help="onde salvar a transcrição legível "
                        "(padrão: transcricao.txt na pasta do vídeo)")
    p.add_argument("--refazer", action="store_true",
                   help="ignora o JSON em cache e chama a API de novo")
    args = p.parse_args()

    data = transcribe(args.camera, args.refazer)
    txt = args.txt or Path(args.camera).resolve().with_name("transcricao.txt")
    n = write_txt(txt, words_of(data))
    print(f"✔ {n} frases em {txt}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        sys.exit(f"Erro rodando {e.cmd[0]}: {e.stderr}")
