#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["numpy", "elevenlabs", "opencv-python-headless>=4.8,<5", "requests"]
# ///
"""
enviar_n8n.py — manda o vídeo editado para o n8n gerar título, descrição/legenda e capa.

O que faz (a partir do projeto.json, depois da edição aprovada):
  1. Carrega e valida o context.json da skill.
  2. Limpa a transcrição crua da ElevenLabs (sem spacing/logprob, sem falsos
     começos "--") e remapeia as frases para o tempo do vídeo editado.
  3. Escolhe os melhores frames de rosto da câmera.
  4. Junta os assets usados na edição (imagens inseridas, fontes, ícones,
     logos citados na fala) e a imagem de referência da capa.
  5. Grava tudo em n8n_<projeto>/ ao lado do vídeo e, sem --dry-run, envia em
     multipart para o webhook (Authorization: Bearer <token da env var>).
  6. Lê a resposta síncrona: salva capa, copy.md e resposta.json.

Uso:
  uv run enviar_n8n.py projeto.json --dry-run
  uv run enviar_n8n.py projeto.json --frase "frase forte" --nota "é publi da X"
  uv run enviar_n8n.py projeto.json --listar-retakes
"""

import argparse
import base64
import hashlib
import json
import mimetypes
import re
import shutil
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import contexto
import frames
import transcricao_limpa as tl
from cortes import fmt_tc, load_projeto, windows_path

SKILL = Path(__file__).resolve().parent.parent
FONTE_RENDER = SKILL / "templates" / "fontes" / "Montserrat.ttf"
ICONES = SKILL / "templates" / "icones"
RASTER = {".jpg", ".jpeg", ".png", ".webp"}
VERSAO_PAYLOAD = 1


# ---------------------------------------------------------------- utilidades

def slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "video"


def video_id(cam):
    h = hashlib.sha1(f"{cam['path']}|{cam['duration']:.3f}".encode()).hexdigest()[:8]
    return f"{slug(cam['path'].stem)}-{h}"


def bloco_capa(modo):
    return "thumbnail" if modo == "youtube" else "capa"


def tabela_retakes(log):
    if not log:
        print("  nenhum falso começo com \"--\" na transcrição")
        return
    for r in log:
        situacao = "já cortado na edição" if r["na_edicao"] else "⚠ AINDA NO VÍDEO"
        print(f"  {fmt_tc(r['inicio'])} → {fmt_tc(r['fim'])}  [{r['regra']}]  "
              f"\"{r['texto_removido']}\"  — {situacao}")


def copiar_asset(origem, destino, lado_max=1280):
    """Copia o asset; imagens raster maiores que lado_max são reduzidas."""
    if origem.suffix.lower() in RASTER:
        import cv2
        img = cv2.imread(str(origem), cv2.IMREAD_UNCHANGED)
        if img is not None and max(img.shape[:2]) > lado_max:
            f = lado_max / max(img.shape[:2])
            img = cv2.resize(img, (int(round(img.shape[1] * f)), int(round(img.shape[0] * f))),
                             interpolation=cv2.INTER_AREA)
            cv2.imwrite(str(destino), img)
            return
    shutil.copy2(origem, destino)


# ------------------------------------------------------------ assets usados

def classificar(caminho, ctx):
    a = ctx["global"]["assets"]
    for chave, tipo in (("memes_dir", "meme"), ("logos_dir", "logo"), ("broll_dir", "broll")):
        d = a[chave]
        if d:
            try:
                caminho.resolve().relative_to(contexto.resolver(d).resolve())
                return tipo
            except ValueError:
                pass
    return "imagem"


def logos_citados(texto, ctx):
    """Logos do context.json cujo nome aparece na fala ("claude", "chat gpt")."""
    palavras = tl.normaliza(texto).split()
    juntas = set(palavras) | {a + b for a, b in zip(palavras, palavras[1:])}
    return {k: v for k, v in ctx["global"]["assets"]["logos"].items()
            if tl.normaliza(k).replace(" ", "") in juntas}


def assets_usados(projeto, ctx, segs, texto):
    """[(entrada do payload, caminho local)] sem duplicatas."""
    itens = {}

    def add(caminho, tipo, origem, intervalo=None, credito=None):
        p = Path(caminho)
        chave = str(p.resolve()) if p.exists() else str(p)
        e = itens.get(chave)
        if e is None:
            e = {"arquivo": p.name, "tipo": tipo, "origem": origem}
            if not p.exists():
                e["faltando"] = True
            itens[chave] = e
        if intervalo:
            e.setdefault("tempo_editado", []).append(intervalo)
        if credito:
            e["credito"] = credito

    for m in (projeto.get("efeitos") or {}).get("imagens", []):
        p = Path(m["arquivo"])
        a, z = tl.para_editado(m["inicio"], segs), tl.para_editado(m["fim"], segs)
        intervalo = [round(a, 3), round(z, 3)] if a is not None and z is not None else None
        add(p, classificar(p, ctx), "efeitos.imagens", intervalo, m.get("credito"))

    if projeto["modo"] == "reels":
        add(FONTE_RENDER, "fonte", "legenda")
    else:
        redes = projeto.get("redes")
        if redes and any(s.get("tipo") == "redes" for s in projeto.get("sobreposicoes", [])):
            add(FONTE_RENDER, "fonte", "card_redes")
            for plat in redes.get("plataformas", []):
                add(ICONES / f"{plat}.svg", "icone", "card_redes")

    fonte_base = contexto.achar_fonte(ctx["global"]["tipografia_base"]["fonte"],
                                      ctx["global"]["assets"]["fontes_dir"])
    if fonte_base:
        add(fonte_base, "fonte", "tipografia_base")

    for nome, caminho in logos_citados(texto, ctx).items():
        add(contexto.resolver(caminho), "logo", f"mencionado_na_fala:{nome}")

    out = []
    for chave, e in itens.items():
        out.append((e, None if e.get("faltando") else Path(chave)))
    return out


# ----------------------------------------------------------------- resposta

def ler_resposta(resp, pasta, ctx, modo, timeout):
    import requests
    try:
        dados = resp.json()
    except ValueError:
        (pasta / "resposta.txt").write_text(resp.text, encoding="utf-8")
        sys.exit("✖ O n8n respondeu, mas não com JSON (salvo em resposta.txt).")
    capa = dados.get("capa") or {}
    salvo = {k: v for k, v in dados.items() if k != "capa"}
    salvo["capa"] = {k: v for k, v in capa.items() if k != "base64"}
    (pasta / "resposta.json").write_text(json.dumps(salvo, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    arq_capa = None
    if capa.get("base64") or capa.get("url"):
        if capa.get("base64"):
            conteudo = base64.b64decode(capa["base64"])
            ext = capa.get("formato", "png")
        else:
            r = requests.get(capa["url"], timeout=timeout)
            r.raise_for_status()
            conteudo = r.content
            ext = (mimetypes.guess_extension(r.headers.get("Content-Type", "").split(";")[0])
                   or ".png").lstrip(".")
        arq_capa = pasta / f"capa.{ext.replace('jpeg', 'jpg')}"
        arq_capa.write_bytes(conteudo)
        try:
            import cv2
            import numpy as np
            img = cv2.imdecode(np.frombuffer(conteudo, np.uint8), cv2.IMREAD_UNCHANGED)
            cfg = ctx[modo][bloco_capa(modo)]
            if img is not None and (img.shape[1], img.shape[0]) != (cfg["largura"], cfg["altura"]):
                print(f"  ⚠ capa veio {img.shape[1]}x{img.shape[0]}; "
                      f"o context.json pede {cfg['largura']}x{cfg['altura']}")
        except ImportError:
            pass

    copy = ctx[modo]["copy"]
    titulo = dados.get("titulo") or ""
    texto = dados.get("descricao") or dados.get("legenda") or ""
    if modo == "youtube" and len(titulo) > copy["max_caracteres_titulo"]:
        print(f"  ⚠ título com {len(titulo)} caracteres (máximo {copy['max_caracteres_titulo']})")
    if modo == "reels" and len(texto) > copy["max_caracteres_legenda"]:
        print(f"  ⚠ legenda com {len(texto)} caracteres (máximo {copy['max_caracteres_legenda']})")

    md = []
    if titulo:
        md += ["## Título", "", titulo, ""]
    if texto:
        md += ["## " + ("Descrição" if modo == "youtube" else "Legenda"), "", texto, ""]
    if dados.get("capitulos"):
        md += ["## Capítulos", ""] + [f"{c.get('tempo', '')} {c.get('titulo', '')}"
                                      for c in dados["capitulos"]] + [""]
    if dados.get("hashtags"):
        md += ["## Hashtags", "", " ".join(dados["hashtags"]), ""]
    (pasta / "copy.md").write_text("\n".join(md), encoding="utf-8")

    print("✔ Resposta do n8n salva")
    if titulo:
        print(f"  Título: {titulo}")
    print(f"  Copy: {windows_path((pasta / 'copy.md').resolve()) or pasta / 'copy.md'}")
    if arq_capa:
        print(f"  Capa: {windows_path(arq_capa.resolve()) or arq_capa}")


# --------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("projeto", help="projeto.json (youtube ou reels) já editado")
    p.add_argument("--frase", action="append", default=[],
                   help="frase de destaque (repetível); vazio = o n8n decide")
    p.add_argument("--nota", action="append", default=[],
                   help="contexto específico deste vídeo (repetível)")
    p.add_argument("--dry-run", action="store_true",
                   help="gera payload e frames na pasta local, sem enviar")
    p.add_argument("--contexto", default=None, help="outro context.json")
    p.add_argument("--listar-retakes", action="store_true",
                   help="só lista os falsos começos \"--\" e se a edição já os corta")
    args = p.parse_args()

    projeto_path = Path(args.projeto).resolve()
    projeto, cam, _ = load_projeto(projeto_path)
    modo = projeto["modo"]
    if not projeto.get("transcricao") or not Path(projeto["transcricao"]).exists():
        sys.exit("✖ O projeto não aponta para uma transcrição existente "
                 "(campo \"transcricao\"). Rode o transcrever.py/cortes.py sem --sem-api.")
    bruto = json.loads(Path(projeto["transcricao"]).read_text(encoding="utf-8"))
    r = tl.processar(bruto, projeto)

    if args.listar_retakes:
        print(f"→ Falsos começos em {Path(projeto['transcricao']).name}:")
        tabela_retakes(r["retakes"])
        return

    ctx, _ = contexto.carregar(args.contexto, exigir_envio=not args.dry_run)
    pasta = projeto_path.parent / f"n8n_{projeto_path.stem}"
    pasta.mkdir(exist_ok=True)
    print(f"→ Transcrição limpa: {len(r['segmentos'])} frases, "
          f"{fmt_tc(r['duracao_final_s'])} de vídeo editado")
    tabela_retakes(r["retakes"])
    fora = [x for x in r["retakes"] if not x["na_edicao"]]

    segs = tl.segmentos(projeto)
    escolhidos, candidatos = frames.melhores_frames(
        projeto, cam["path"], lambda t: tl.para_editado(t, segs) or 0.0,
        ctx[modo]["frames"], pasta)

    usados = assets_usados(projeto, ctx, segs, r["texto_limpo"])
    pasta_assets = pasta / "assets"
    if pasta_assets.exists():
        shutil.rmtree(pasta_assets)
    pasta_assets.mkdir()
    anexos = []
    for i, (e, caminho) in enumerate(usados, 1):
        e["campo"] = f"asset_{i:02d}"
        if caminho:
            copiar_asset(caminho, pasta_assets / caminho.name)
            anexos.append((e["campo"], pasta_assets / caminho.name))
        else:
            print(f"  ⚠ asset não encontrado no disco, vai só o nome: {e['arquivo']} ({e['origem']})")

    ref = ctx[modo][bloco_capa(modo)]["referencia_thumb"]
    ref_nome = None
    if ref and contexto.resolver(ref).is_file():
        ref_local = pasta / Path(ref).name
        shutil.copy2(contexto.resolver(ref), ref_local)
        ref_nome = ref_local.name
    else:
        print(f"  ⚠ {modo}.{bloco_capa(modo)}.referencia_thumb não existe: "
              f"{contexto.resolver(ref) if ref else '(vazio)'}; vai sem referência")

    frases_destaque = []
    alvo = tl.normaliza(r["texto_limpo"])
    for f in args.frase:
        if tl.normaliza(f) not in alvo:
            print(f"  ⚠ frase de destaque não aparece no vídeo editado: \"{f}\"")
        frases_destaque.append(f)

    payload = {
        "video_id": video_id(cam),
        "modo": modo,
        "duracao_final_s": r["duracao_final_s"],
        "texto_limpo": r["texto_limpo"],
        "segmentos": r["segmentos"],
        "frases_destaque": frases_destaque,
        "frames": [{"arquivo": m["arquivo"], "tempo_editado": m["tempo_editado"],
                    "score": m["score"]} for m in escolhidos],
        "referencia_thumb": ref_nome,
        "assets_usados": [e for e, _ in usados],
        "sons_usados": sorted({s["som"] for s in (projeto.get("efeitos") or {}).get("sons", [])
                               if s.get("som")}),
        "contexto": {**contexto.para_payload(ctx, modo), "notas_do_video": args.nota},
        "retakes_fora_da_edicao": fora,
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "versao_payload": VERSAO_PAYLOAD,
    }
    corpo = json.dumps(payload, ensure_ascii=False, indent=1)
    (pasta / "payload.json").write_text(corpo, encoding="utf-8")
    (pasta / "retakes.json").write_text(json.dumps(r["retakes"], ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    (pasta / "candidatos.json").write_text(json.dumps(candidatos, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    multipart = [("payload", ("payload.json", corpo.encode(), "application/json"))]
    for m in escolhidos:
        multipart.append((m["arquivo"].removesuffix(".jpg"),
                          (m["arquivo"], (pasta / "frames" / m["arquivo"]).read_bytes(),
                           "image/jpeg")))
    if ref_nome:
        multipart.append(("referencia_thumb", (ref_nome, (pasta / ref_nome).read_bytes(),
                                               mimetypes.guess_type(ref_nome)[0]
                                               or "application/octet-stream")))
    for campo, arq in anexos:
        multipart.append((campo, (arq.name, arq.read_bytes(),
                                  mimetypes.guess_type(arq.name)[0] or "application/octet-stream")))

    print(f"✔ Payload em {windows_path(pasta.resolve()) or pasta}")
    print(f"  campos do multipart: {', '.join(c for c, _ in multipart)}")
    if args.dry_run:
        print("  (--dry-run: nada foi enviado)")
        return

    import requests
    n8n = ctx["global"]["n8n"]
    timeout = n8n.get("timeout_s", 300)
    print(f"→ Enviando para o n8n (aguardando a resposta, até {timeout}s)...", flush=True)
    resp = requests.post(n8n["webhook_url"], files=multipart, timeout=timeout,
                         headers={"Authorization": f"Bearer {contexto.token(n8n['token_env_var'])}"})
    if not resp.ok:
        sys.exit(f"✖ O n8n respondeu {resp.status_code}: {resp.text[:500]}\n"
                 f"  O payload continua em {pasta} para reenviar.")
    ler_resposta(resp, pasta, ctx, modo, timeout)


if __name__ == "__main__":
    try:
        main()
    except contexto.ContextoInvalido as e:
        sys.exit(str(e))
