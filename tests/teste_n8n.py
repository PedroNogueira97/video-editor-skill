# /// script
# requires-python = ">=3.9"
# dependencies = ["numpy", "elevenlabs", "opencv-python-headless>=4.8,<5", "requests"]
# ///
"""Testa context.json, a limpeza da transcrição e o envio para o n8n.

Só dados sintéticos: nenhuma transcrição real vai para o repositório. Para
rodar também contra um vídeo real (local), defina:
  TRANSCRICAO_REAL=/mnt/e/.../Posicionamento.transcricao.json
  PROJETO_REAL=/mnt/e/.../projeto.json

Uso: uv run --script tests/teste_n8n.py [pasta_de_trabalho]
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

AQUI = Path(__file__).resolve().parent
SCRIPTS = AQUI.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import contexto  # noqa: E402
import transcricao_limpa as tl  # noqa: E402

FALHAS = []


def confere(cond, msg):
    print(("  ok   " if cond else "  FALHOU ") + msg)
    if not cond:
        FALHAS.append(msg)


# ------------------------------------------------- transcrição sintética

def transcricao(frases):
    """[(t_inicio, "texto com palavras")] → JSON no formato da ElevenLabs,
    com spacing e logprob. Cada palavra dura 0,25 s, sem pausa dentro da frase."""
    words, texto = [], []
    for t, frase in frases:
        for p in frase.split():
            words.append({"text": p, "start": round(t, 3), "end": round(t + 0.25, 3),
                          "type": "word", "logprob": -0.01})
            words.append({"text": " ", "start": round(t + 0.25, 3), "end": round(t + 0.25, 3),
                          "type": "spacing", "logprob": -0.001})
            t += 0.25
            texto.append(p)
    return {"text": " ".join(texto), "words": words, "language_code": "por"}


FRASES = [
    (0.5, "Hoje eu vou falar de café."),
    (3.0, "porque foi a melhor bebida que ex--"),          # repetição de 5 palavras
    (5.0, "porque foi a melhor bebida que eu já tomei."),
    (9.0, "Os grãos, a torra e o mo--"),                   # pontuação colada ("grãos,")
    (11.0, "Os grãos a torra e a água fazem diferença."),
    (16.0, "Me s--"),                                      # sem repetição: recua até a pausa
    (17.0, "segue pra mais dicas de café."),
    (21.0, "Isso aqui foi cortado na edição."),            # take removido no projeto
    (25.0, "E aqui eu falo sem parar e no meio digo um tre-- e continuo falando."),
]


def projeto_sintetico(gancho=None):
    blocos = [
        {"inicio": 0.4, "fim": 2.4, "tela": False, "removido": False, "motivo": ""},
        {"inicio": 2.9, "fim": 4.9, "tela": False, "removido": True, "motivo": "T1"},
        {"inicio": 4.9, "fim": 7.9, "tela": False, "removido": False, "motivo": ""},
        {"inicio": 8.9, "fim": 10.9, "tela": False, "removido": True, "motivo": "T2"},
        {"inicio": 10.9, "fim": 14.0, "tela": False, "removido": False, "motivo": ""},
        {"inicio": 15.9, "fim": 16.6, "tela": False, "removido": True, "motivo": "T3"},
        {"inicio": 16.9, "fim": 19.0, "tela": False, "removido": False, "motivo": ""},
        {"inicio": 20.9, "fim": 23.0, "tela": False, "removido": True, "motivo": "C1"},
        {"inicio": 24.9, "fim": 29.5, "tela": False, "removido": False, "motivo": ""},
    ]
    return {"modo": "reels", "blocos": blocos, "gancho": gancho}


def teste_transcricao():
    print("== transcrição: ruído, retakes, frases, tempo editado")
    data = transcricao(FRASES)
    r = tl.processar(data, projeto_sintetico())
    texto = json.dumps(r, ensure_ascii=False)
    confere('"spacing"' not in texto and "logprob" not in texto, "sem spacing nem logprob")
    regras = {x["palavra_gatilho"]: x for x in r["retakes"]}
    confere(regras["ex--"]["regra"] == "repeticao" and regras["ex--"]["inicio"] == 3.0,
            "ex--: removido da repetição anterior até o --")
    confere(regras["mo--"]["regra"] == "repeticao", "mo--: casa a repetição mesmo com vírgula")
    confere(regras["s--"]["regra"] == "pausa"
            and regras["s--"]["texto_removido"] == "Me s--", "s--: recua até a pausa")
    confere(regras["tre--"]["regra"] == "incerto"
            and regras["tre--"]["texto_removido"] == "tre--",
            "tre--: sem pausa por perto, remove só a palavra (incerto)")
    t = r["texto_limpo"]
    confere(t.count("porque foi a melhor bebida que eu já tomei.") == 1 and "ex--" not in t,
            "fica só a versão completa da frase")
    confere("Os grãos a torra e a água fazem diferença." in t and "mo--" not in t,
            "fica a frase refeita dos grãos")
    confere("segue pra mais dicas" in t and "Me s--" not in t, "Me s-- sai")
    confere("cortado na edição" not in t, "take removido no projeto não entra no texto")
    s = r["segmentos"]
    confere(all(a["inicio_editado"] < b["inicio_editado"] for a, b in zip(s, s[1:])),
            "inicio_editado cresce de forma monotônica")
    confere(all(x["inicio_editado"] <= x["inicio_original"] for x in s),
            "inicio_editado ≤ inicio_original")
    confere(all(x["na_edicao"] for x in r["retakes"] if x["palavra_gatilho"] != "tre--")
            and not regras["tre--"]["na_edicao"],
            "cobertura: retakes cortados x o que ainda está no vídeo")

    print("== transcrição: gancho")
    rg = tl.processar(data, projeto_sintetico(gancho={"inicio": 11.0, "fim": 14.0}))
    sg = rg["segmentos"]
    confere(sg[0].get("gancho") and sg[0]["inicio_editado"] == 0.0, "gancho é o primeiro segmento")
    confere(rg["texto_limpo"].count("fazem diferença") == 1, "texto do gancho não se repete")


def teste_real():
    tr, pr = os.environ.get("TRANSCRICAO_REAL"), os.environ.get("PROJETO_REAL")
    if not (tr and pr and Path(tr).exists() and Path(pr).exists()):
        print("== real: pulado (defina TRANSCRICAO_REAL e PROJETO_REAL para rodar)")
        return
    print(f"== real: {Path(tr).name}")
    r = tl.processar(json.loads(Path(tr).read_text()), json.loads(Path(pr).read_text()))
    t = r["texto_limpo"]
    confere("ti--" not in t and t.count("porque foi o último console em que eu tive acesso") == 1,
            "ti-- removido; fica a frase completa")
    confere("o f--" not in t and "e os nossos maiores ídolos disponibilizando tudo de graça" in t,
            "o f-- removido")
    confere("Me s--" not in t and "já me segue e compartilha" in t, "Me s-- removido")
    s = r["segmentos"]
    confere(all(a["inicio_editado"] < b["inicio_editado"] for a, b in zip(s, s[1:]))
            and all(x["inicio_editado"] <= x["inicio_original"] for x in s), "invariantes de tempo")


# ------------------------------------------------------------- contexto

def teste_contexto(pasta):
    print("== context.json")
    ex = json.loads((AQUI.parent / "context.example.json").read_text(encoding="utf-8"))
    v = contexto.validar(ex)
    confere(not v.erros, "context.example.json passa na validação")
    confere(any(a.startswith("reels.capa.referencia_thumb:") for a in v.avisos),
            "caminho de asset ausente vira aviso (com o campo), não erro")
    ruim = json.loads(json.dumps(ex))
    ruim["youtube"]["thumbnail"]["contorno"]["espessura_px"] = "8px"
    ruim["reels"]["capa"]["tarjas"][1]["cor_tarja"] = "azul"
    del ruim["global"]["identidade"]["cor_marca"]
    erros = contexto.validar(ruim).erros
    confere(any(e.startswith("youtube.thumbnail.contorno.espessura_px:") for e in erros),
            "erro aponta youtube.thumbnail.contorno.espessura_px")
    confere(any(e.startswith("reels.capa.tarjas[1].cor_tarja:") for e in erros),
            "erro aponta reels.capa.tarjas[1].cor_tarja")
    confere(any(e.startswith("global.identidade.cor_marca:") and "ausente" in e for e in erros),
            "erro de campo obrigatório ausente")
    try:
        contexto.carregar(pasta / "nao_existe.json")
        confere(False, "context.json ausente para com erro")
    except SystemExit as e:
        confere("cp " in str(e) and "context.example.json" in str(e),
                "context.json ausente manda copiar o example")
    seg = contexto.para_payload(ex, "reels")
    confere(set(seg) == {"global", "reels"} and "n8n" not in seg["global"],
            "payload: global sem n8n + só o modo atual")


# --------------------------------------------- dry-run e envio (ponta a ponta)

class N8nFalso(BaseHTTPRequestHandler):
    recebido = {}

    def log_message(self, *a):
        pass

    def do_POST(self):
        corpo = self.rfile.read(int(self.headers["Content-Length"]))
        N8nFalso.recebido = {"auth": self.headers.get("Authorization"),
                             "tipo": self.headers.get("Content-Type"), "corpo": corpo}
        png = base64.b64encode(subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=1080x1920",
             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, check=True).stdout).decode()
        resp = json.dumps({"titulo": "", "legenda": "Legenda gerada", "hashtags": ["#cafe"],
                           "capa": {"base64": png, "formato": "png"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


def teste_envio(pasta):
    print("== enviar_n8n.py: --dry-run e envio para um n8n falso")
    video = pasta / "camera.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=30",
                    "-f", "lavfi", "-i", "sine=f=440:d=30", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(video)], check=True)
    (pasta / "camera.transcricao.json").write_text(json.dumps(transcricao(FRASES)),
                                                   encoding="utf-8")
    img = pasta / "imagem.png"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=red:s=2000x1000", "-frames:v", "1", str(img)], check=True)
    proj = projeto_sintetico()
    proj.update(camera={"arquivo": str(video), "windows": None, "duracao": 30.0, "fps": "30/1",
                        "largura": 1080, "altura": 1920, "audio": True, "audio_rate": 44100,
                        "audio_canais": 1, "criado": None},
                tela=None, transcricao=str(pasta / "camera.transcricao.json"),
                efeitos={"imagens": [{"inicio": 5.0, "fim": 6.0, "arquivo": str(img)},
                                     {"inicio": 12.0, "fim": 13.0, "arquivo": str(img)},
                                     {"inicio": 17.0, "fim": 18.0,
                                      "arquivo": str(pasta / "sumiu.png")}],
                         "sons": [{"t": 1.0, "som": "pop"}]})
    pp = pasta / "projeto.json"
    pp.write_text(json.dumps(proj), encoding="utf-8")

    srv = HTTPServer(("127.0.0.1", 0), N8nFalso)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ctx = json.loads((AQUI.parent / "context.example.json").read_text(encoding="utf-8"))
    ctx["global"]["n8n"]["webhook_url"] = f"http://127.0.0.1:{srv.server_port}/webhook"
    ctx["global"]["n8n"]["token_env_var"] = "TOKEN_TESTE_N8N"
    ref = pasta / "referencia.jpg"
    shutil.copy2(img, ref)
    ctx["reels"]["capa"]["referencia_thumb"] = str(ref)
    ctx["reels"]["frames"] = {"quantidade_enviada": 3, "candidatos_analisados": 8}
    cp = pasta / "context.json"
    cp.write_text(json.dumps(ctx, ensure_ascii=False), encoding="utf-8")

    cmd = ["uv", "run", "--quiet", "--script", str(SCRIPTS / "enviar_n8n.py"), str(pp),
           "--contexto", str(cp), "--frase", "Hoje eu vou falar de café"]
    env = {**os.environ, "TOKEN_TESTE_N8N": "segredo-teste"}
    seco = subprocess.run(cmd + ["--dry-run"], env=env, capture_output=True, text=True)
    confere(seco.returncode == 0, "--dry-run termina sem erro" + ("" if seco.returncode == 0
                                                                 else "\n" + seco.stderr[-800:]))
    saida = pasta / "n8n_projeto"
    payload = json.loads((saida / "payload.json").read_text(encoding="utf-8"))
    confere(N8nFalso.recebido == {}, "--dry-run não faz chamada HTTP")
    confere(len(list((saida / "frames").glob("frame_*.jpg"))) == 3, "3 frames gravados")
    confere(payload["referencia_thumb"] == "referencia.jpg", "referencia_thumb no payload")
    tipos = [(a["arquivo"], a.get("faltando", False)) for a in payload["assets_usados"]]
    confere(tipos.count(("imagem.png", False)) == 1 and ("sumiu.png", True) in tipos,
            "assets: imagem usada 2x vai uma vez; ausente marcada faltando")
    confere(payload["sons_usados"] == ["pop"], "sons só pelo nome")
    confere(set(payload["contexto"]) == {"global", "reels", "notas_do_video"}
            and "n8n" not in payload["contexto"]["global"], "contexto sem n8n e sem youtube")
    confere("segredo-teste" not in json.dumps(payload), "token fora do payload")
    confere(payload["frases_destaque"] == ["Hoje eu vou falar de café"], "frases_destaque")

    envio = subprocess.run(cmd, env=env, capture_output=True, text=True)
    confere(envio.returncode == 0, "envio termina sem erro" + ("" if envio.returncode == 0
                                                               else "\n" + envio.stderr[-800:]))
    rec = N8nFalso.recebido
    confere(rec.get("auth") == "Bearer segredo-teste", "Authorization: Bearer <token>")
    corpo = rec.get("corpo", b"")
    for campo in ("payload", "frame_01", "frame_03", "referencia_thumb", "asset_01"):
        confere(f'name="{campo}"'.encode() in corpo, f"multipart tem o campo {campo}")
    confere(b"base64" not in corpo.split(b'name="frame_01"')[0][-200:], "frames como arquivo")
    confere((saida / "capa.png").exists() and (saida / "copy.md").exists(),
            "resposta: capa.png e copy.md salvos")
    srv.shutdown()


def main():
    pasta = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="teste_n8n_"))
    pasta.mkdir(parents=True, exist_ok=True)
    teste_transcricao()
    teste_real()
    teste_contexto(pasta)
    teste_envio(pasta)
    print(f"\n{'✔ tudo certo' if not FALHAS else f'✖ {len(FALHAS)} falha(s)'}")
    sys.exit(1 if FALHAS else 0)


if __name__ == "__main__":
    main()
