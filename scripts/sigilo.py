#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["numpy", "elevenlabs", "rapidocr", "onnxruntime", "opencv-python-headless>=4.8"]
# ///
"""
sigilo.py — modo youtube: acha dados sigilosos na gravação da tela para borrar.

O que faz:
  1. Escolhe quadros da tela para ler: sempre que a tela muda o bastante (e no
     máximo a cada --intervalo-max s), pelo proxy leve.
  2. Lê o texto de cada quadro no original (RapidOCR, modelos tiny, só CPU),
     só nos trechos da tela que entram no vídeo (blocos não removidos). Só
     reconhece as caixas de texto novas: barra lateral, menus etc. vêm do cache.
     Se você restaurar takes na timeline, rode de novo (o já lido é reaproveitado).
     O resultado fica em ocr_tela.json (dá para parar e continuar).
  3. Procura e-mail, telefone, CPF/CNPJ e tokens/chaves (suspeitos).
  4. Grava em projeto.json → "borroes": cada texto encontrado com as caixas e
     os intervalos (tempo da TELA) em que aparece. Começam ativos; desative
     ("ativo": false) o que não for sigiloso. A timeline mostra os borrões e o
     render aplica.

Uso:
  uv run sigilo.py projeto.json
  uv run sigilo.py projeto.json --so-listar        # só mostra o que já achou
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from cortes import fmt_tc, load_projeto, save_projeto

PADROES = [
    ("email", re.compile(r"[\w.+-]{2,}@[\w-]{2,}")),
    ("cpf", re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b")),
    ("cnpj", re.compile(r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b")),
    ("telefone", re.compile(r"(\+?55\s?)?\(?\b\d{2}\)?[\s.-]?9?\d{4}[\s.-]?\d{4}\b")),
    ("token", re.compile(r"\b(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])"
                         r"[A-Za-z0-9_\-]{28,}\b")),
]


def classificar(txt):
    for tipo, rx in PADROES:
        if rx.search(txt):
            return tipo
    return None


# ------------------------------------------------- 1. quadros a ler

def janelas_usadas(projeto, dur_tela):
    """Trechos da TELA que podem entrar no vídeo: os blocos não removidos
    (com ou sem tela ligada, já que dá para ligar com T na timeline)."""
    off, iv = projeto["offset"], []
    for b in projeto["blocos"]:
        if b["removido"]:
            continue
        a, z = max(0.0, b["inicio"] - off), min(dur_tela, b["fim"] - off)
        if z <= a:
            continue
        if iv and a - iv[-1][1] < 2.0:
            iv[-1][1] = max(iv[-1][1], z)
        else:
            iv.append([a, z])
    return iv


def escolher_tempos(video, dur, intervalo_max, limiar, janelas, fps=2):
    """Lê um quadro no começo de cada trecho usado e sempre que a tela muda o
    bastante desde a última leitura (ou a cada intervalo_max s)."""
    W, H = 160, 84
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vf",
                          f"fps={fps},scale={W}:{H},format=gray", "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    fr = np.frombuffer(raw, np.uint8).reshape(-1, H, W).astype(np.int16)
    tempos = []
    for a, z in janelas:
        i0, i1 = int(np.ceil(a * fps)), min(len(fr) - 1, int(z * fps))
        if i0 > i1:
            continue
        tempos.append(round(i0 / fps, 2))
        ref = fr[i0]
        for i in range(i0 + 1, i1 + 1):
            t = i / fps
            mudou = np.abs(fr[i] - ref).mean()
            if (mudou > limiar and t - tempos[-1] >= 1.0) or t - tempos[-1] >= intervalo_max:
                tempos.append(round(t, 2))
                ref = fr[i]
    return sorted({t for t in tempos if t < dur - 0.2})


# ------------------------------------------------------- 2. OCR

class Leitor:
    """Modelos 'tiny' do PP-OCRv6: ~3s por quadro nesta CPU (os 'small' levam ~13s)
    e ainda leem e-mails e telefones. Caixas iguais às já lidas vêm do cache."""

    def __init__(self):
        from rapidocr import ModelType, RapidOCR
        self.eng = RapidOCR(params={"Det.model_type": ModelType.TINY,
                                    "Rec.model_type": ModelType.TINY,
                                    "Global.log_level": "warning"})
        self.cache = {}

    def ler(self, img):
        det = self.eng(img, use_det=True, use_cls=False, use_rec=False)
        boxes = [] if det.boxes is None else det.boxes
        caixas, novos = [], {}
        for b in boxes:
            x0, y0 = np.floor(b.min(axis=0)).astype(int)
            x1, y1 = np.ceil(b.max(axis=0)).astype(int)
            x0, y0 = max(0, x0), max(0, y0)
            crop = img[y0:y1, x0:x1]
            if crop.size == 0:
                continue
            chave = hashlib.md5(crop.tobytes() + str(crop.shape).encode()).hexdigest()
            if chave not in self.cache:
                novos[chave] = np.ascontiguousarray(crop)
            caixas.append((chave, [int(x0), int(y0), int(x1 - x0), int(y1 - y0)]))
        if novos:                                     # reconhece as novas de uma vez
            r = self.eng.recognize_txt(list(novos.values()))
            for chave, txt in zip(novos, r.txts):
                self.cache[chave] = txt
        return [[self.cache[c], caixa] for c, caixa in caixas if self.cache[c].strip()]


def quadro(video, t, w, h):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video),
                          "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                         capture_output=True, check=True).stdout
    if len(raw) < w * h * 3:
        return None
    return np.frombuffer(raw, np.uint8)[: w * h * 3].reshape(h, w, 3)


# ------------------------------------------- 3. ocorrências → borrões

def iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter / (aw * ah + bw * bh - inter + 1e-9)


def montar_borroes(leituras, tempos, dur, pad):
    """Cada achado vale do quadro lido anterior até o próximo (conservador)."""
    grupos = {}
    for i, t in enumerate(tempos):
        ini = tempos[i - 1] if i > 0 else 0.0
        fim = tempos[i + 1] if i + 1 < len(tempos) else dur
        for txt, (x, y, w, h) in leituras.get(str(t), []):
            tipo = classificar(txt)
            if not tipo:
                continue
            chave = (tipo, re.sub(r"\s+", "", txt.lower()))
            g = grupos.setdefault(chave, {"tipo": tipo, "texto": txt, "ativo": True,
                                          "ocorrencias": []})
            caixa = [max(0, x - pad), max(0, y - pad), w + 2 * pad, h + 2 * pad]
            for oc in g["ocorrencias"]:
                if oc["fim"] >= ini - 1e-6 and iou(caixa, [oc["x"], oc["y"], oc["w"], oc["h"]]) > 0.3:
                    oc["fim"] = max(oc["fim"], round(fim, 2))
                    break
            else:
                g["ocorrencias"].append({"ini": round(ini, 2), "fim": round(fim, 2),
                                         "x": caixa[0], "y": caixa[1],
                                         "w": caixa[2], "h": caixa[3]})
    borroes = sorted(grupos.values(), key=lambda g: g["ocorrencias"][0]["ini"])
    for i, g in enumerate(borroes):
        g["id"] = f"b{i + 1}"
    return borroes


def listar(projeto):
    off = projeto["offset"]
    print(f"\n{'id':4} {'tipo':9} {'ativo':5} {'tempo na tela':>9}  primeira vez (câmera)  texto")
    for g in projeto.get("borroes", []):
        total = sum(o["fim"] - o["ini"] for o in g["ocorrencias"])
        primeira = g["ocorrencias"][0]["ini"] + off
        print(f"{g['id']:4} {g['tipo']:9} {'sim' if g['ativo'] else 'não':5} "
              f"{total:8.0f}s  {fmt_tc(primeira)[:8]:>21}  {g['texto']}")


# ------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("projeto")
    p.add_argument("--intervalo-max", type=float, default=15,
                   help="lê pelo menos um quadro a cada N s de tela (padrão 15)")
    p.add_argument("--limiar", type=float, default=2.0,
                   help="mudança mínima na tela para ler outro quadro (padrão 2)")
    p.add_argument("--margem", type=int, default=8, help="px a mais em volta do texto")
    p.add_argument("--so-listar", action="store_true")
    args = p.parse_args()

    projeto, _, scr = load_projeto(args.projeto)
    if not scr:
        sys.exit("Erro: este projeto não tem gravação de tela.")
    if args.so_listar:
        listar(projeto)
        return
    pasta = Path(args.projeto).resolve().parent
    proxy = pasta / "proxies" / "tela_480p.mp4"
    cache_path = pasta / "ocr_tela.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    print("→ Escolhendo quadros da tela para ler...", flush=True)
    janelas = janelas_usadas(projeto, scr["duration"])
    print(f"  {sum(z - a for a, z in janelas) / 60:.0f} min de tela entram no vídeo "
          f"(de {scr['duration'] / 60:.0f} min)")
    tempos = escolher_tempos(proxy if proxy.exists() else scr["path"], scr["duration"],
                             args.intervalo_max, args.limiar, janelas)
    faltam = [t for t in tempos if str(t) not in cache]
    print(f"→ {len(tempos)} quadros ({len(tempos) - len(faltam)} já lidos); "
          "lendo o texto (OCR na CPU)...", flush=True)
    leitor, t0 = (Leitor() if faltam else None), time.time()
    for i, t in enumerate(faltam, 1):
        img = quadro(scr["path"], t, scr["width"], scr["height"])
        cache[str(t)] = leitor.ler(img) if img is not None else []
        if i % 5 == 0 or i == len(faltam):
            cache_path.write_text(json.dumps(cache, ensure_ascii=False))
            el = time.time() - t0
            print(f"  [{i}/{len(faltam)}] tela {fmt_tc(t)[:8]}  decorrido {fmt_tc(el)[:8]}, "
                  f"faltam ~{fmt_tc(el / i * (len(faltam) - i))[:8]}", flush=True)

    projeto, _, _ = load_projeto(args.projeto)      # relê: a timeline pode ter salvo
    antigos = {(b["tipo"], b["texto"]): b["ativo"] for b in projeto.get("borroes", [])}
    projeto["borroes"] = montar_borroes(cache, tempos, scr["duration"], args.margem)
    for b in projeto["borroes"]:                     # preserva o que você já desativou
        b["ativo"] = antigos.get((b["tipo"], b["texto"]), True)
    save_projeto(args.projeto, projeto)
    print(f"\n✔ {len(projeto['borroes'])} textos sigilosos salvos em projeto.json")
    listar(projeto)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        sys.exit(f"Erro rodando {e.cmd[0]}: {e.stderr}")
