"""
Self-Test fuer providers.factory (ohne Framework:
python test/test_factory_opts.py).

Prueft, dass get_provider() die optionalen Decoder-/Engine-Parameter an den
lokalen Provider durchreicht (nur gesetzte Keys, unbekannte werden gefiltert)
und der OpenAI-Pfad davon unberuehrt bleibt. Die echten Provider werden durch
Stubs ersetzt, damit KEIN Whisper-Modell geladen wird.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers import factory  # noqa: E402

fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    if not ok:
        fails += 1
    print(f"[{'OK ' if ok else 'XX '}] {label}: got={got!r} want={want!r}")


class StubLocal:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class StubOpenAI:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


# Echte Provider durch Stubs ersetzen (kein Modell-Load).
factory.LocalWhisperProvider = StubLocal
factory.OpenAIProvider = StubOpenAI

# Lokal: gesetzte Decoder-Optionen werden durchgereicht ...
p = factory.get_provider(
    "local", model="small", condition_on_previous_text=False,
    repetition_penalty=1.1, vad_filter=False,
    unbekannt="wird_gefiltert")
check("model_size durchgereicht", p.kwargs.get("model_size"), "small")
check("condition_on_previous_text durchgereicht",
      p.kwargs.get("condition_on_previous_text"), False)
check("repetition_penalty durchgereicht", p.kwargs.get("repetition_penalty"), 1.1)
check("vad_filter durchgereicht", p.kwargs.get("vad_filter"), False)
check("unbekannter Key gefiltert", "unbekannt" in p.kwargs, False)

# ... und nicht gesetzte Optionen bleiben weg (Provider-Default greift).
p2 = factory.get_provider("local", model="medium")
check("nur model_size gesetzt", sorted(p2.kwargs), ["model_size"])

# OpenAI-Pfad ignoriert lokale Optionen.
po = factory.get_provider("openai", model="gpt-4o-transcribe",
                          condition_on_previous_text=False)
check("openai bekommt model", po.kwargs.get("model"), "gpt-4o-transcribe")
check("openai ohne lokale Opts",
      "condition_on_previous_text" in po.kwargs, False)

print()
if fails:
    print(f"FEHLGESCHLAGEN: {fails} Pruefung(en).")
    sys.exit(1)
print("Alle Pruefungen bestanden.")
