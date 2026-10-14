"""
frames.py — escolhe os melhores frames de rosto da câmera para a capa/thumbnail.

O que faz:
  1. Janelas válidas: só onde a câmera é a fonte ativa (youtube: blocos ativos
     sem tela; reels: blocos ativos menos os trechos com imagem inserida).
  2. Espalha N candidatos pela duração EDITADA dessas janelas e extrai cada um
     do arquivo original da câmera (sem legenda nem sobreposições).
  3. Pontua: rosto grande (YuNet), olhos abertos (Haar) e nitidez (variância
     do Laplaciano). Frame sem rosto vale 20%.
  4. Escolhe os melhores com distância mínima entre eles e grava JPGs com lado
     maior ≤ 1280 px.

O YuNet é o mesmo detector do reels_compor.py (o MediaPipe não roda nesta CPU).
"""

import subprocess
from pathlib import Path

MODELO_ROSTO = ("https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
                "face_detection_yunet/face_detection_yunet_2023mar.onnx")
PESOS = {"rosto": 0.45, "olhos": 0.25, "nitidez": 0.30}
LARGURA_ANALISE = 640


def janelas_camera(projeto):
    """Intervalos (tempo original) em que só a câmera aparece, na ordem do vídeo."""
    ativos = [(b["inicio"], b["fim"]) for b in sorted(projeto["blocos"], key=lambda b: b["inicio"])
              if not b["removido"] and not (projeto["modo"] == "youtube" and b["tela"])]
    if projeto["modo"] != "reels":
        return ativos
    inserts = []
    for m in (projeto.get("efeitos") or {}).get("imagens", []):
        inserts.append((m["inicio"], m["fim"]))
    out = []
    for a, z in ativos:
        pedacos = [(a, z)]
        for s, e in inserts:
            novo = []
            for x, y in pedacos:
                if e <= x or s >= y:
                    novo.append((x, y))
                    continue
                if s > x:
                    novo.append((x, s))
                if e < y:
                    novo.append((e, y))
            pedacos = novo
        out.extend((x, y) for x, y in pedacos if y - x > 0.2)
    return out


def tempos_candidatos(janelas, n):
    """n tempos (original) no centro de n fatias iguais da duração somada das
    janelas, para os candidatos seguirem o vídeo como ele é assistido."""
    total = sum(z - a for a, z in janelas)
    if total <= 0 or n <= 0:
        return []
    out = []
    for i in range(n):
        alvo = (i + 0.5) * total / n
        acum = 0.0
        for a, z in janelas:
            if alvo < acum + (z - a):
                out.append(a + alvo - acum)
                break
            acum += z - a
    return out


def extrair(camera, t, destino):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}",
                    "-i", str(camera), "-frames:v", "1", "-q:v", "2", str(destino)],
                   check=True)
    return destino


def detectores():
    """(YuNet, Haar de olhos); cada um vira None se não carregar.
    O Haar saiu do OpenCV 5: por isso o enviar_n8n.py fixa opencv < 5."""
    import cv2
    modelo = Path.home() / ".cache" / "editar-video" / "face_detection_yunet_2023mar.onnx"
    if not modelo.exists():
        modelo.parent.mkdir(parents=True, exist_ok=True)
        print("  baixando o modelo de rosto YuNet (230 KB)...")
        subprocess.run(["curl", "-fsSL", "-o", str(modelo), MODELO_ROSTO], check=True)
    yunet = olhos = None
    try:
        yunet = cv2.FaceDetectorYN.create(str(modelo), "", (320, 320), 0.6)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ detector de rosto não carregou ({e}); pontuando só pela nitidez")
    try:
        olhos = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_eye.xml")
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ detector de olhos não carregou ({e}); sem a nota de olhos abertos")
    return yunet, olhos


def medir(img_path, yunet, olhos):
    """Notas brutas de um frame: área do rosto (fração), olhos (0/0.5/1), nitidez."""
    import cv2
    img = cv2.imread(str(img_path))
    h, w = img.shape[:2]
    escala = LARGURA_ANALISE / w
    peq = cv2.resize(img, (LARGURA_ANALISE, int(round(h * escala))))
    cinza = cv2.cvtColor(peq, cv2.COLOR_BGR2GRAY)
    area, nota_olhos, recorte = 0.0, 0.0, cinza
    if yunet is not None:
        yunet.setInputSize((peq.shape[1], peq.shape[0]))
        _, faces = yunet.detect(peq)
        if faces is not None and len(faces):
            x, y, fw, fh = (int(v) for v in max(faces, key=lambda f: f[2] * f[3])[:4])
            x, y = max(0, x), max(0, y)
            area = fw * fh / (peq.shape[0] * peq.shape[1])
            recorte = cinza[y:y + fh, x:x + fw]
            topo = cinza[y:y + fh // 2, x:x + fw]
            if topo.size and olhos is not None:
                achados = olhos.detectMultiScale(topo, 1.1, 4, minSize=(max(8, fw // 10),) * 2)
                nota_olhos = min(len(achados), 2) / 2
    nitidez = float(cv2.Laplacian(recorte if recorte.size else cinza, cv2.CV_64F).var())
    return {"rosto_area": round(area, 4), "olhos": nota_olhos, "nitidez": round(nitidez, 1)}


def pontuar(medidas):
    """Notas brutas → score 0..1, normalizando área e nitidez pelo maior valor."""
    max_area = max((m["rosto_area"] for m in medidas), default=0) or 1
    max_nit = max((m["nitidez"] for m in medidas), default=0) or 1
    for m in medidas:
        s = (PESOS["rosto"] * m["rosto_area"] / max_area + PESOS["olhos"] * m["olhos"]
             + PESOS["nitidez"] * m["nitidez"] / max_nit)
        m["score"] = round(s if m["rosto_area"] > 0 else s * 0.2, 4)
    return medidas


def escolher(medidas, n, duracao_editada):
    """Os n melhores por score, com distância mínima no tempo editado."""
    dist = duracao_editada / (2 * n) if n else 0
    ordem = sorted(medidas, key=lambda m: -m["score"])
    out = []
    for m in ordem:
        if all(abs(m["tempo_editado"] - o["tempo_editado"]) >= dist for o in out):
            out.append(m)
        if len(out) == n:
            return out
    for m in ordem:                      # poucos candidatos: completa sem a distância
        if m not in out:
            out.append(m)
        if len(out) == n:
            break
    return out


def salvar_jpg(origem, destino, lado_max=1280):
    import cv2
    img = cv2.imread(str(origem))
    h, w = img.shape[:2]
    f = min(1.0, lado_max / max(h, w))
    if f < 1:
        img = cv2.resize(img, (int(round(w * f)), int(round(h * f))), interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(destino), img, [cv2.IMWRITE_JPEG_QUALITY, 90])


def melhores_frames(projeto, camera, para_editado, cfg, pasta, lado_max=1280):
    """Pipeline completo. para_editado(t) → tempo editado. Devolve (escolhidos, candidatos)."""
    janelas = janelas_camera(projeto)
    tempos = tempos_candidatos(janelas, cfg["candidatos_analisados"])
    if not tempos:
        print("  ⚠ nenhum trecho só de câmera para tirar frames")
        return [], []
    print(f"→ Analisando {len(tempos)} frames da câmera (rosto, olhos, nitidez)...", flush=True)
    tmp = pasta / "candidatos"
    tmp.mkdir(parents=True, exist_ok=True)
    yunet, olhos = detectores()
    medidas = []
    for i, t in enumerate(tempos, 1):
        img = extrair(camera, t, tmp / f"cand_{i:02d}.jpg")
        m = medir(img, yunet, olhos)
        m.update(tempo_original=round(t, 3), tempo_editado=round(para_editado(t), 3),
                 _img=img)
        medidas.append(m)
    pontuar(medidas)
    total = sum(z - a for a, z in janelas)
    escolhidos = escolher(medidas, cfg["quantidade_enviada"], total)
    dest = pasta / "frames"
    dest.mkdir(parents=True, exist_ok=True)
    for f in dest.glob("frame_*.jpg"):
        f.unlink()
    for k, m in enumerate(escolhidos, 1):
        m["arquivo"] = f"frame_{k:02d}.jpg"
        salvar_jpg(m["_img"], dest / m["arquivo"], lado_max)
    for m in medidas:
        m.pop("_img", None)
    for f in tmp.glob("cand_*.jpg"):
        f.unlink()
    tmp.rmdir()
    com_rosto = sum(m["rosto_area"] > 0 for m in medidas)
    print(f"  rosto em {com_rosto}/{len(medidas)} candidatos; {len(escolhidos)} escolhidos")
    return escolhidos, medidas
