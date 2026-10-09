"""Mock do SDK da ElevenLabs para os testes: devolve palavras falsas
alinhadas aos trechos com som do vídeo sintético e conta as chamadas."""

import os
from pathlib import Path

# trechos com fala no vídeo sintético (ver teste_sintetico.sh)
FALA = [(0.5, 5.0), (7.0, 12.0), (12.3, 16.0), (19.0, 25.0), (25.5, 29.5)]
TEXTO = ("então hoje vou mostrar como montar a timeline. vou falar de novo. "
         "hoje vou mostrar como montar a timeline no resolve. primeiro abrimos "
         "o terminal e rodamos o script. depois importamos o arquivo no resolve. "
         "e pronto o corte está feito.").split()


class _Resp:
    def __init__(self, words):
        self._d = {"language_code": "por", "text": " ".join(w["text"] for w in words
                                                             if w["type"] == "word"),
                   "words": words}

    def model_dump(self, mode=None):
        return self._d


class _STT:
    def convert(self, file, model_id, timestamps_granularity, language_code, **kw):
        assert model_id == "scribe_v2", model_id
        assert timestamps_granularity == "word"
        assert language_code == "por"
        assert file.read(3), "arquivo de áudio vazio"
        contador = os.environ.get("MOCK_CONTADOR")
        if contador:
            p = Path(contador)
            p.write_text(str(int(p.read_text() or 0) + 1) if p.exists() else "1")

        words, i = [], 0
        for a, b in FALA:
            t = a
            while t + 0.4 <= b + 1e-6:
                words.append({"text": TEXTO[i % len(TEXTO)], "start": round(t, 3),
                              "end": round(t + 0.4, 3), "type": "word"})
                words.append({"text": " ", "start": round(t + 0.4, 3),
                              "end": round(t + 0.5, 3), "type": "spacing"})
                i += 1
                t += 0.5
        # como a API real às vezes faz: palavra esticada por cima do silêncio 5s–7s
        next(w for w in words if w["start"] == 4.5)["end"] = 6.9
        words.append({"text": "(risos)", "start": 17.0, "end": 18.0,
                      "type": "audio_event"})
        return _Resp(words)


class ElevenLabs:
    def __init__(self, api_key):
        assert api_key
        self.speech_to_text = _STT()
