"""
transcricao_limpa.py — transcrição crua da ElevenLabs + cortes do projeto.json →
frases no tempo do vídeo editado e texto limpo. Funções puras (sem ffmpeg nem rede).

Etapas:
  1. palavras():   só type == "word", sem logprob, ordenadas.
  2. retakes():    falsos começos marcados com "--" (frase abandonada).
  3. frases():     quebra em . ? ! … ou pausa > 1,0 s (no tempo ORIGINAL).
  4. remapear():   tempo original → tempo editado, pelos mesmos segmentos dos
                   renders; o que caiu num corte sai.
  5. texto_limpo(): o texto corrido do vídeo editado, sem retakes.

O JSON cru nunca é alterado: ele continua em <video>.transcricao.json.
"""

import re

FIM_DE_FRASE = (".", "?", "!", "…")
PONTUACAO = re.compile(r"[.,;:!?…\"'()\[\]]|--")

JANELA_BUSCA = 20.0     # s para trás em que se procura a repetição
PAUSA_RETAKE = 0.5      # s: pausa que delimita o começo da frase abandonada
RECUO_MAX_PALAVRAS = 6  # limites do recuo até a pausa (senão, "incerto")
RECUO_MAX_S = 3.0
PAUSA_FRASE = 1.0       # s
COBERTURA_MIN = 0.9     # fração do retake fora do vídeo para contar como "na edição"


# ------------------------------------------------------------- 1. palavras

def palavras(data):
    """Só as palavras faladas: {text, start, end}, sem spacing nem logprob."""
    out = []
    for w in data.get("words") or []:
        if w.get("type", "word") != "word" or w.get("start") is None or w.get("end") is None:
            continue
        out.append({"text": w["text"].strip(), "start": float(w["start"]),
                    "end": float(w["end"])})
    out.sort(key=lambda w: w["start"])
    return out


def normaliza(s):
    return PONTUACAO.sub("", s.lower()).strip()


# -------------------------------------------------------------- 2. retakes

def retakes(ws):
    """Acha falsos começos ("ti--"). Devolve (índices removidos, log)."""
    remover, log = set(), []
    norm = [normaliza(w["text"]) for w in ws]
    for k, w in enumerate(ws):
        if not w["text"].endswith("--"):
            continue
        seguintes = norm[k + 1:k + 6]
        inicio, regra = None, None
        for n in range(min(5, len(seguintes)), 2, -1):
            alvo = seguintes[:n]
            for i in range(k - n, -1, -1):
                if ws[k]["start"] - ws[i]["start"] > JANELA_BUSCA:
                    break
                if norm[i:i + n] == alvo:
                    inicio, regra = i, "repeticao"
                    break
            if inicio is not None:
                break
        if inicio is None:
            i = k
            while i > 0 and ws[i]["start"] - ws[i - 1]["end"] <= PAUSA_RETAKE:
                i -= 1
            inicio, regra = i, "pausa"
            if k - i + 1 > RECUO_MAX_PALAVRAS or ws[k]["end"] - ws[i]["start"] > RECUO_MAX_S:
                inicio, regra = k, "incerto"
        remover.update(range(inicio, k + 1))
        log.append({"inicio": round(ws[inicio]["start"], 3), "fim": round(ws[k]["end"], 3),
                    "texto_removido": " ".join(x["text"] for x in ws[inicio:k + 1]),
                    "regra": regra, "palavra_gatilho": w["text"]})
    return remover, log


# ------------------------------------------------------------- 3. frases

def frases(ws):
    """Listas de palavras; quebra em pontuação final ou pausa > PAUSA_FRASE."""
    out, cur = [], []
    for w in ws:
        if cur and w["start"] - cur[-1]["end"] > PAUSA_FRASE:
            out.append(cur)
            cur = []
        cur.append(w)
        if w["text"].endswith(FIM_DE_FRASE):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


# ------------------------------------------------- 4. tempo editado

def segmentos(projeto):
    """Trechos na ordem do vídeo final: [(inicio, fim, gancho?)], tempo original.
    Reels: gancho (se houver) + blocos ativos; youtube: blocos ativos."""
    segs = []
    g = projeto.get("gancho") if projeto.get("modo") == "reels" else None
    if g:
        segs.append((g["inicio"], g["fim"], True))
    for b in sorted(projeto["blocos"], key=lambda b: b["inicio"]):
        if not b["removido"] and b["fim"] > b["inicio"]:
            segs.append((b["inicio"], b["fim"], False))
    return segs


def para_editado(t, segs, gancho=False):
    """Tempo original → editado; None se cair fora dos trechos mantidos."""
    t0 = 0.0
    for a, z, eh_gancho in segs:
        if eh_gancho == gancho and a <= t < z:
            return t0 + t - a
        t0 += z - a
    return None


def duracao(segs):
    return sum(z - a for a, z, _ in segs)


def remapear(lista_frases, segs):
    """Frases (tempo original) → segmentos do payload. Palavra fica se o ponto
    médio dela cai num trecho mantido (o mesmo critério da legenda do Reels)."""
    out = []
    for gancho in (True, False):
        if gancho and not any(s[2] for s in segs):
            continue
        for fr in lista_frases:
            vivas = [(w, para_editado((w["start"] + w["end"]) / 2, segs, gancho)) for w in fr]
            vivas = [(w, t) for w, t in vivas if t is not None]
            if not vivas:
                continue
            w0, w1 = vivas[0][0], vivas[-1][0]
            meio0 = (w0["start"] + w0["end"]) / 2
            ini_ed = vivas[0][1] - (meio0 - w0["start"])
            meio1 = (w1["start"] + w1["end"]) / 2
            fim_ed = vivas[-1][1] + (w1["end"] - meio1)
            seg = {"inicio_editado": round(max(0.0, ini_ed), 3),
                   "fim_editado": round(fim_ed, 3),
                   "inicio_original": round(w0["start"], 3),
                   "fim_original": round(w1["end"], 3),
                   "texto": " ".join(w["text"] for w, _ in vivas)}
            if gancho:
                seg["gancho"] = True
            out.append(seg)
    out.sort(key=lambda s: s["inicio_editado"])
    return out


def cobertura(log, segs, ws):
    """Marca em cada retake se a edição já o tira do vídeo (≥ COBERTURA_MIN)."""
    for r in log:
        dur = r["fim"] - r["inicio"]
        dentro = sum(max(0.0, min(z, r["fim"]) - max(a, r["inicio"]))
                     for a, z, g in segs if not g)
        r["na_edicao"] = dur <= 0 or (dur - dentro) / dur >= COBERTURA_MIN
    return log


# ------------------------------------------------------------ 5. tudo junto

def texto_limpo(segs_payload):
    return " ".join(s["texto"] for s in segs_payload if not s.get("gancho"))


def processar(data, projeto):
    """JSON cru + projeto → {segmentos, texto_limpo, retakes, duracao_final_s}."""
    ws = palavras(data)
    remover, log = retakes(ws)
    limpas = [w for i, w in enumerate(ws) if i not in remover]
    segs = segmentos(projeto)
    payload_segs = remapear(frases(limpas), segs)
    return {"segmentos": payload_segs, "texto_limpo": texto_limpo(payload_segs),
            "retakes": cobertura(log, segs, ws),
            "duracao_final_s": round(duracao(segs), 3)}
