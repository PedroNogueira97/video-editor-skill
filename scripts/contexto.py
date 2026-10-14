#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""
contexto.py — carrega e valida o context.json (identidade visual e de copy do canal).

O que faz:
  1. Acha o context.json: --contexto, EDITAR_VIDEO_CONTEXTO ou a raiz da skill.
     Se não existir, para mandando copiar o context.example.json.
  2. Valida campos obrigatórios, tipos, cores em hex e valores permitidos, e
     lista TODOS os erros de uma vez, com o caminho exato do campo.
  3. Caminhos de asset (pastas, logos, referência da thumb) que não existem
     viram AVISO, não erro: a skill segue sem eles.
  4. Caminhos relativos resolvem a partir da raiz da skill.

Uso (só valida e mostra o resultado):
  uv run contexto.py
  uv run contexto.py --contexto outro.json
"""

import argparse
import difflib
import json
import os
import re
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
PADRAO = SKILL / "context.json"
EXEMPLO = SKILL / "context.example.json"

COR = re.compile(r"^#(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")
VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
POSICOES = {"topo", "base", "centro", "esquerda", "direita"}
PESOS = {"regular": 400, "bold": 700, "black": 900}
EXT_FONTE = (".ttf", ".otf", ".woff2", ".woff")


class ContextoInvalido(SystemExit):
    pass


# ----------------------------------------------------------- tipos de campo
# Cada validador recebe (valor, caminho, v) e registra erros/avisos em v.

def texto(vazio=True):
    def f(x, c, v):
        if not isinstance(x, str):
            v.erro(c, "esperado texto", x)
        elif not vazio and not x.strip():
            v.erro(c, "não pode ficar vazio", x)
    return f


def inteiro(minimo=None, maximo=None):
    def f(x, c, v):
        if isinstance(x, bool) or not isinstance(x, int):
            v.erro(c, "esperado inteiro", x)
        elif (minimo is not None and x < minimo) or (maximo is not None and x > maximo):
            faixa = f"entre {minimo} e {maximo}" if maximo is not None else f"≥ {minimo}"
            v.erro(c, f"esperado inteiro {faixa}", x)
    return f


def numero(minimo=0.0, estrito=False):
    def f(x, c, v):
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            v.erro(c, "esperado número", x)
        elif x < minimo or (estrito and x == minimo):
            v.erro(c, f"esperado número {'>' if estrito else '≥'} {minimo:g}", x)
    return f


def booleano(x, c, v):
    if not isinstance(x, bool):
        v.erro(c, "esperado true ou false", x)


def cor(x, c, v):
    if not isinstance(x, str) or not COR.match(x):
        v.erro(c, "esperado cor #RRGGBB ou #RRGGBBAA", x)


def cor_ou_nenhum(x, c, v):
    if x != "nenhum":
        if not isinstance(x, str) or not COR.match(x):
            v.erro(c, 'esperado "nenhum" ou cor #RRGGBB/#RRGGBBAA', x)


def posicao(x, c, v):
    if x not in POSICOES:
        v.erro(c, f"esperado um de {', '.join(sorted(POSICOES))}", x)


def peso(x, c, v):
    if isinstance(x, str) and x.lower() in PESOS:
        return
    try:
        n = int(x)
    except (TypeError, ValueError):
        n = None
    if isinstance(x, bool) or n is None or not 100 <= n <= 900:
        v.erro(c, 'esperado 100–900 (número ou texto, ex. "900") ou regular/bold/black', x)


def lista(item, minimo=0):
    def f(x, c, v):
        if not isinstance(x, list):
            v.erro(c, "esperado lista", x)
            return
        if len(x) < minimo:
            v.erro(c, f"esperado lista com pelo menos {minimo} item(ns)", x)
        for i, e in enumerate(x):
            item(e, f"{c}[{i}]", v)
    return f


def hashtag(x, c, v):
    if not isinstance(x, str) or not re.match(r"^#\w+", x):
        v.erro(c, "esperada hashtag começando com #", x)


def par_de_numeros(x, c, v):
    if (not isinstance(x, list) or len(x) != 2
            or any(isinstance(n, bool) or not isinstance(n, (int, float)) for n in x)):
        v.erro(c, "esperada lista de 2 números, ex. [0, 6]", x)


def url(x, c, v):
    if not isinstance(x, str) or not re.match(r"^https?://[^\s/]+", x):
        v.erro(c, "esperada URL http:// ou https://", x)


def nome_var(x, c, v):
    if not isinstance(x, str) or not VAR.match(x):
        v.erro(c, "esperado nome de variável de ambiente (letras, números e _)", x)


def pasta(x, c, v):
    texto()(x, c, v)
    if isinstance(x, str) and x.strip() and not resolver(x).is_dir():
        v.aviso(c, f"pasta não existe: {resolver(x)}")


def arquivo(x, c, v):
    texto()(x, c, v)
    if isinstance(x, str) and x.strip() and not resolver(x).is_file():
        v.aviso(c, f"arquivo não existe: {resolver(x)}")


def mapa_de_arquivos(x, c, v):
    if not isinstance(x, dict):
        v.erro(c, "esperado objeto {nome: caminho}", x)
        return
    for k, e in x.items():
        arquivo(e, f"{c}.{k}", v)


def obj(campos):
    """Objeto com todos os campos obrigatórios; campos a mais viram aviso."""
    def f(x, c, v):
        if not isinstance(x, dict):
            v.erro(c, "esperado objeto { ... }", x)
            return
        for k, val in campos.items():
            sub = f"{c}.{k}" if c else k
            if k not in x:
                v.erro(sub, "campo obrigatório ausente")
            else:
                val(x[k], sub, v)
        for k in x:
            if k not in campos:
                parecido = difflib.get_close_matches(k, campos, n=1)
                dica = f" (quis dizer {parecido[0]!r}?)" if parecido else ""
                v.aviso(f"{c}.{k}" if c else k, f"campo desconhecido, ignorado{dica}")
    return f


def frames(x, c, v):
    obj({"quantidade_enviada": inteiro(1), "candidatos_analisados": inteiro(1)})(x, c, v)
    if (isinstance(x, dict) and isinstance(x.get("quantidade_enviada"), int)
            and isinstance(x.get("candidatos_analisados"), int)
            and x["quantidade_enviada"] > x["candidatos_analisados"]):
        v.erro(f"{c}.quantidade_enviada", "não pode ser maior que candidatos_analisados",
               x["quantidade_enviada"])


CONTORNO = obj({"cor": cor, "espessura_px": numero(0)})

ESQUEMA = obj({
    "global": obj({
        "canal": obj({"nome": texto(vazio=False), "handle_instagram": texto(),
                      "handle_youtube": texto()}),
        "tom_de_voz": texto(vazio=False),
        "identidade": obj({"cor_primaria": cor, "cor_secundaria": cor, "cor_marca": cor}),
        "tipografia_base": obj({"fonte": texto(vazio=False), "altura_linha": numero(0, True),
                                "contorno": CONTORNO}),
        "assets": obj({"fontes_dir": pasta, "memes_dir": pasta, "logos_dir": pasta,
                       "broll_dir": pasta, "logos": mapa_de_arquivos}),
        "n8n": obj({"webhook_url": texto(), "token_env_var": nome_var}),
    }),
    "youtube": obj({
        "thumbnail": obj({
            "largura": inteiro(1), "altura": inteiro(1), "referencia_thumb": arquivo,
            "fonte": texto(vazio=False), "peso": peso,
            "cor_texto": cor, "cor_destaque": cor, "contorno": CONTORNO,
            "sombra": obj({"cor": cor, "blur_px": numero(0), "offset_px": par_de_numeros}),
            "posicao_texto": posicao, "max_palavras": inteiro(1, 10),
            "composicao": texto(), "instrucoes_extra": texto(),
        }),
        "copy": obj({
            "max_caracteres_titulo": inteiro(1, 100), "exemplos_titulos": lista(texto()),
            "exemplos_texto_thumb": lista(texto()), "descricao_rodape": texto(),
            "gerar_capitulos": booleano, "hashtags_fixas": lista(hashtag),
        }),
        "frames": frames,
    }),
    "reels": obj({
        "capa": obj({
            "largura": inteiro(1), "altura": inteiro(1), "referencia_thumb": arquivo,
            "fonte": texto(vazio=False), "peso": peso, "estilo_texto": texto(vazio=False),
            "tarjas": lista(obj({"cor_tarja": cor, "cor_texto": cor})),
            "posicao_texto": posicao, "max_palavras": inteiro(1, 20),
            "max_linhas": inteiro(1, 6),
            "zona_segura": obj({"topo_px": inteiro(0), "base_px": inteiro(0),
                                "laterais_px": inteiro(0),
                                "grid_perfil_3x4": obj({"topo_px": inteiro(0),
                                                        "base_px": inteiro(0)})}),
            "instrucoes_extra": texto(),
        }),
        "legenda": obj({"fonte": texto(vazio=False), "altura_linha": numero(0, True),
                        "cor": cor, "contorno": CONTORNO, "fundo": cor_ou_nenhum,
                        "posicao": posicao}),
        "copy": obj({
            "max_caracteres_legenda": inteiro(1, 2200), "exemplos_legendas": lista(texto()),
            "exemplos_texto_thumb": lista(texto()), "cta": texto(),
            "hashtags_fixas": lista(hashtag),
        }),
        "frames": frames,
    }),
})


# ---------------------------------------------------------------- validação

class Validacao:
    def __init__(self):
        self.erros, self.avisos = [], []

    def erro(self, caminho, msg, valor=...):
        veio = "" if valor is ... else f", veio {json.dumps(valor, ensure_ascii=False)[:60]}"
        self.erros.append(f"{caminho}: {msg}{veio}")

    def aviso(self, caminho, msg):
        self.avisos.append(f"{caminho}: {msg}")


def resolver(caminho):
    p = Path(caminho).expanduser()
    return p if p.is_absolute() else SKILL / p


def achar_fonte(nome, fontes_dir):
    """Arquivo de fonte pelo nome ("Typewriter" → assets/fontes/Typewriter.ttf), ou None."""
    p = resolver(nome)
    if p.is_file():
        return p
    d = resolver(fontes_dir) if fontes_dir else None
    if not d or not d.is_dir():
        return None
    alvo = re.sub(r"[\s_-]", "", nome.lower())
    for f in sorted(d.rglob("*")):
        if f.suffix.lower() in EXT_FONTE and re.sub(r"[\s_-]", "", f.stem.lower()) == alvo:
            return f
    return None


def validar(dados, exigir_envio=False):
    v = Validacao()
    ESQUEMA(dados, "", v)
    if v.erros:
        return v
    g = dados["global"]
    # fontes nomeadas (não descritas): procura o arquivo em fontes_dir
    for c, nome in [("global.tipografia_base.fonte", g["tipografia_base"]["fonte"]),
                    ("reels.legenda.fonte", dados["reels"]["legenda"]["fonte"])]:
        if not achar_fonte(nome, g["assets"]["fontes_dir"]):
            v.aviso(c, f"fonte {nome!r} não encontrada em {resolver(g['assets']['fontes_dir'])}")
    if exigir_envio:
        url(g["n8n"]["webhook_url"], "global.n8n.webhook_url", v)
        var = g["n8n"]["token_env_var"]
        if not token(var):
            v.erro("global.n8n.token_env_var",
                   f"a variável {var} não está definida (no ambiente ou no .env da skill)")
    return v


def token(var):
    """Valor da variável no ambiente ou no .env da skill. Nunca é impresso."""
    if os.environ.get(var):
        return os.environ[var]
    env = SKILL / ".env"
    if env.exists():
        for linha in env.read_text(encoding="utf-8").splitlines():
            k, _, val = linha.partition("=")
            if k.strip() == var and val.strip():
                return val.strip().strip("'\"")
    return None


def carregar(caminho=None, exigir_envio=False, silencioso=False):
    """Lê e valida o context.json. Para com erro claro se faltar ou estiver inválido."""
    p = Path(caminho or os.environ.get("EDITAR_VIDEO_CONTEXTO") or PADRAO)
    if not p.is_file():
        raise ContextoInvalido(
            f"✖ Não achei o context.json em {p}\n"
            f"  Copie o exemplo e preencha:  cp {EXEMPLO} {PADRAO}")
    try:
        dados = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ContextoInvalido(f"✖ {p} não é um JSON válido: linha {e.lineno}, "
                               f"coluna {e.colno}: {e.msg}") from None
    v = validar(dados, exigir_envio)
    if v.erros:
        raise ContextoInvalido(
            f"✖ {p.name} inválido ({len(v.erros)} erro{'s' if len(v.erros) > 1 else ''}):\n"
            + "\n".join(f"  {e}" for e in v.erros))
    if v.avisos and not silencioso:
        print(f"⚠ {p.name}: {len(v.avisos)} aviso(s)")
        for a in v.avisos:
            print(f"  {a}")
    return dados, v.avisos


def para_payload(dados, modo):
    """global (sem o bloco n8n) + só o bloco do modo, com caminhos reduzidos ao nome."""
    def nomes(x, chave=""):
        if isinstance(x, dict):
            return {k: nomes(val, k) for k, val in x.items()}
        if isinstance(x, list):
            return [nomes(e, chave) for e in x]
        if isinstance(x, str) and x and (chave.endswith("_dir") or chave == "referencia_thumb"):
            return Path(x).name
        return x
    g = nomes({k: val for k, val in dados["global"].items() if k != "n8n"})
    g["assets"]["logos"] = {k: Path(val).name
                            for k, val in dados["global"]["assets"]["logos"].items()}
    return {"global": g, modo: nomes(dados[modo])}


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--contexto", default=None)
    p.add_argument("--envio", action="store_true",
                   help="valida também webhook_url e a variável do token")
    args = p.parse_args()
    _, avisos = carregar(args.contexto, exigir_envio=args.envio)
    print(f"✔ válido ({len(avisos)} aviso(s))")


if __name__ == "__main__":
    main()
