"""
Self-Test fuer providers.factory (ohne Framework:
python test/test_factory_opts.py).

Prueft, dass get_provider() ALLE optionalen Decoder-/Engine-Parameter an den
lokalen Provider durchreicht (nur gesetzte Keys, unbekannte werden gefiltert)
und der OpenAI-Pfad davon unberuehrt bleibt. Die echten Provider werden durch
Stubs ersetzt, damit KEIN Whisper-Modell geladen wird.

Das Stubben passiert bewusst NUR in main() (mit Wiederherstellung im finally),
nicht beim Import — sonst wuerde ein Test-Sammellauf die globalen
factory-Klassen fuer nachfolgende Tests verbiegen.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers import factory  # noqa: E402


class StubLocal:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class StubOpenAI:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def main() -> int:
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        if not ok:
            fails += 1
        print(f"[{'OK ' if ok else 'XX '}] {label}: got={got!r} want={want!r}")

    orig_local = factory.LocalWhisperProvider
    orig_openai = factory.OpenAIProvider
    factory.LocalWhisperProvider = StubLocal
    factory.OpenAIProvider = StubOpenAI
    try:
        # Lokal: ALLE Keys aus _LOCAL_OPT_KEYS werden durchgereicht ...
        local_opts = dict(
            device="cpu", compute_type="int8", vad_filter=False,
            condition_on_previous_text=False, repetition_penalty=1.1,
            compression_ratio_threshold=2.0, temperature=0.0)
        # Sicherstellen, dass der Test bei neuen Keys mitwaechst:
        check("Test deckt alle _LOCAL_OPT_KEYS ab",
              sorted(local_opts), sorted(factory._LOCAL_OPT_KEYS))

        p = factory.get_provider(
            "local", model="small", unbekannt="wird_gefiltert", **local_opts)
        check("model_size durchgereicht", p.kwargs.get("model_size"), "small")
        for k, v in local_opts.items():
            check(f"{k} durchgereicht", p.kwargs.get(k), v)
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
    finally:
        factory.LocalWhisperProvider = orig_local
        factory.OpenAIProvider = orig_openai

    print()
    if fails:
        print(f"FEHLGESCHLAGEN: {fails} Pruefung(en).")
        return 1
    print("Alle Pruefungen bestanden.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
