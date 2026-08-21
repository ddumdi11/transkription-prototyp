"""
Self-Test fuer transcribe.format_run_timing (ohne Framework:
python test/test_transcribe_timing.py).

Prueft den Benchmark-Notiz-String (Dauer + Echtzeitfaktor) inkl. Rundung und
Fallback ohne verlaessliche Audiodauer.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import transcribe  # noqa: E402
from transcribe import format_run_timing, get_audio_duration  # noqa: E402

fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    if not ok:
        fails += 1
    print(f"[{'OK ' if ok else 'XX '}] {label}: got={got!r} want={want!r}")


# Schneller als Echtzeit (RTF < 1), Beispiel aus dem Auftrag (312 s / 760 s).
check("schneller als Echtzeit", format_run_timing(312, 760), "312 s, 0.41x Echtzeit")
# Langsamer als Echtzeit (RTF > 1).
check("langsamer als Echtzeit", format_run_timing(1000, 500), "1000 s, 2.00x Echtzeit")
# Sekunden werden gerundet, RTF auf 2 Nachkommastellen.
check("Rundung Sekunden", format_run_timing(312.7, 760), "313 s, 0.41x Echtzeit")
# Ohne verlaessliche Audiodauer nur die Dauer (ffprobe fehlgeschlagen -> 0).
check("audio_duration=0 -> nur Dauer", format_run_timing(100, 0), "100 s")
check("audio_duration<0 -> nur Dauer", format_run_timing(100, -5), "100 s")

# --- Robustheit: Dauerermittlung darf NIE werfen (Punkt 3) ------------------
# Eine fehlgeschlagene Dauerermittlung muss 0.0 liefern statt zu werfen, damit
# der Transkriptionslauf nicht kippt (Echtzeitfaktor entfaellt dann einfach).

# Reale, nicht existierende Datei: ffprobe scheitert -> 0.0.
check("nicht existierende Datei -> 0.0",
      get_audio_duration(Path("garantiert_nicht_da_1234.xyz")), 0.0)


def _run_raises_filenotfound(*a, **k):
    raise FileNotFoundError("ffprobe nicht installiert")


class _FakeCompleted:
    def __init__(self, stdout):
        self.stdout = stdout


_orig_run = transcribe.subprocess.run
try:
    # ffprobe-Binary fehlt (FileNotFoundError beim Start) -> 0.0, keine Ausnahme.
    transcribe.subprocess.run = _run_raises_filenotfound
    try:
        got = get_audio_duration(Path("egal.wav"))
        raised = False
    except Exception:
        got, raised = None, True
    check("ffprobe fehlt -> keine Ausnahme", raised, False)
    check("ffprobe fehlt -> 0.0", got, 0.0)

    # ffprobe liefert unbrauchbare Ausgabe (defekte Datei) -> 0.0.
    transcribe.subprocess.run = lambda *a, **k: _FakeCompleted("N/A\n")
    check("unbrauchbare ffprobe-Ausgabe -> 0.0",
          get_audio_duration(Path("egal.wav")), 0.0)
finally:
    transcribe.subprocess.run = _orig_run

print()
if fails:
    print(f"FEHLGESCHLAGEN: {fails} Pruefung(en).")
    sys.exit(1)
print("Alle Pruefungen bestanden.")
