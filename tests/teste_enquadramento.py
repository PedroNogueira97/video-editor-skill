# /// script
# requires-python = ">=3.9"
# dependencies = ["numpy", "elevenlabs", "opencv-python-headless>=4.8"]
# ///
"""Testa o reenquadramento vertical do reels_compor.py.

1. O detector de rosto carrega e roda (num degradê: nenhum rosto, sem erro).
   Com ROSTO_VIDEO=<vídeo com uma pessoa>, confere que acha o rosto na maioria
   das amostras.
2. Com um rosto "falso" indo da esquerda para a direita, o recorte acompanha:
   num degradê horizontal, o centro do quadro vertical fica mais claro com o tempo.

Uso: uv run --script teste_enquadramento.py <pasta_de_trabalho>
"""
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import reels_compor as rc  # noqa: E402
from cortes import probe  # noqa: E402

pasta = Path(sys.argv[1])
pasta.mkdir(parents=True, exist_ok=True)
video = pasta / "degrade.mp4"
subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "nullsrc=s=1280x720:r=30:d=10,geq=lum='X/W*255':cb=128:cr=128",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(video)],
               check=True)
cam = probe(video)

# 1. detector de verdade: nada no degradê; rosto num vídeo real, se houver
track = rc.face_track(cam)
assert track is None, "achou rosto num degradê?"
if os.environ.get("ROSTO_VIDEO"):
    real = probe(os.environ["ROSTO_VIDEO"])
    ts_r, xs_r = rc.face_track(real)
    taxa = np.count_nonzero(~np.isnan(xs_r)) / len(xs_r)
    print(f"vídeo real: rosto em {taxa:.0%} das amostras, centro médio {np.nanmean(xs_r):.2f}")
    assert taxa > 0.8, taxa

# 2. rosto falso: centro vai de 0.2 a 0.8 da largura em 10s
ts = np.arange(0, 10, 0.2)
rc.face_track = lambda cam: (ts, 0.2 + 0.6 * ts / 10)
hf = pasta / "hf"
hf.mkdir(exist_ok=True)
base = rc.make_base(cam, {}, hf, True)
info = probe(base)
assert (info["width"], info["height"]) == (1080, 1920), info


def brilho(t):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(base),
                          "-frames:v", "1", "-vf", "crop=2:2:539:959,format=gray",
                          "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    return raw[0]


vals = [brilho(t) for t in (0.5, 3, 6, 9.5)]
print("brilho no centro do quadro vertical ao longo do tempo:", vals)
assert vals[0] < 90 and vals[-1] > 150, vals          # começou à esquerda, terminou à direita
assert all(a <= b + 3 for a, b in zip(vals, vals[1:])), vals   # sempre andando para a direita
assert (hf / "assets" / "crop.cmd").exists()
print("✔ enquadramento ok")
