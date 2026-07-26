"""
Self-Test fuer eval/loop_check (ohne Framework: python test/test_loop_check.py).

Prueft die Wort-Loop-Erkennung gegen die zwei bekannten realen Faelle
(Mai 95x, Aufnahme #402 4x) sowie den Floskel-Ausschluss und sauberen Text.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.loop_check import (  # noqa: E402
    find_repeats,
    phrase_repetition,
    strip_header,
    loops_in_file,
)

fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    if not ok:
        fails += 1
    print(f"[{'OK ' if ok else 'XX '}] {label}: got={got!r} want={want!r}")


def check_true(label, cond):
    global fails
    if not cond:
        fails += 1
    print(f"[{'OK ' if cond else 'XX '}] {label}: {cond}")


# --- Fall 1: Mai-Loop, 95x "Feuerwerkzeug, am 5. November 2013 a. D." -------
mai_phrase = "Feuerwerkzeug, am 5. November 2013 a. D."
mai_text = (mai_phrase + " ") * 95
pr = phrase_repetition(mai_text)
check("Mai top_count", pr["top_count"], 95)
check_true("Mai top_phrase enthaelt 'Feuerwerkzeug'", "Feuerwerkzeug" in pr["top_phrase"])
mai_blocks = [r for r in find_repeats(mai_text) if r["count"] == 95]
check("Mai Block mit count 95 gefunden", len(mai_blocks), 1)
check("Mai Phrasenlaenge (Woerter)", mai_blocks[0]["unit_words"], 7)

# --- Fall 2: #402-Loop, 4x "Oder vielleicht auch irgendwo in der Bank." -----
p402 = "Oder vielleicht auch irgendwo in der Bank."
text402 = ("Das konnte man nicht sehen aber da hatte er eine Jacke an. "
           + (p402 + " ") * 4
           + "Gesellschaft verbandelt das konnte man nicht sehen.")
blocks402 = [r for r in find_repeats(text402)
             if "Bank." in r["phrase"] and r["count"] == 4]
check("#402 Block 4x 'in der Bank' gefunden", len(blocks402), 1)
check("#402 Phrasenlaenge (Woerter)", blocks402[0]["unit_words"], 7)

# --- Floskel-Ausschluss: "ja ja ja ja" ist KEIN Loop bei Schwelle >=3 W. ----
filler = "also ja ja ja ja genau und dann weiter im Text hier."
over_thresh = [r for r in find_repeats(filler)
               if r["unit_words"] >= 3 and r["count"] >= 3]
check("Floskel 'ja ja ja' unter Schwelle -> nicht geflaggt", over_thresh, [])

# --- Sauberer Text: keine Loops ---------------------------------------------
clean = "Ich gehe heute spazieren und denke ueber das Projekt nach."
check("Sauberer Text: keine Wiederholung", find_repeats(clean), [])

# --- strip_header entfernt Metadaten-Kopf -----------------------------------
md = ("# Aufnahme #402\n- Datei: Aufnahme #402.wav\n- Modell: medium (local)\n"
      "- Status: Rohtranskript\n\nDer eigentliche Text beginnt hier.")
body = strip_header(md)
check_true("strip_header behaelt Body", body.strip() == "Der eigentliche Text beginnt hier.")
check_true("strip_header entfernt Kopf", "Modell" not in body and "#" not in body)

# --- loops_in_file: Schwellen greifen auf Dateiebene ------------------------
with tempfile.TemporaryDirectory() as d:
    f = Path(d) / "loop.md"
    f.write_text("# T\n\n" + mai_text, encoding="utf-8")
    res = loops_in_file(f, min_words=3, min_count=3)
    check_true("loops_in_file findet Mai-Loop", any(r["count"] == 95 for r in res))
    f.write_text("# T\n\n" + clean, encoding="utf-8")
    check("loops_in_file: sauber -> leer", loops_in_file(f, 3, 3), [])

# --- Grenzfall Phrasenlaenge: max_len kappt lange Phrasen -------------------
w12 = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
w13 = w12 + " nu"
t12 = (w12 + " ") * 3
t13 = (w13 + " ") * 3

b12 = [r for r in find_repeats(t12) if r["count"] >= 3]        # Default max_len=12
check("12-Wort-Phrase (<=max) erkannt", len(b12), 1)
check("12-Wort-Phrasenlaenge", b12[0]["unit_words"], 12)

b13_default = [r for r in find_repeats(t13) if r["count"] >= 3]  # 13 > max_len 12
check("13-Wort-Phrase bei max_len=12 NICHT erkannt", b13_default, [])
b13_wide = [r for r in find_repeats(t13, max_len=13) if r["count"] >= 3]
check("13-Wort-Phrase bei max_len=13 erkannt", len(b13_wide), 1)
check("13-Wort-Phrasenlaenge", b13_wide[0]["unit_words"], 13)

print()
if fails:
    print(f"FEHLGESCHLAGEN: {fails} Pruefung(en).")
    sys.exit(1)
print("Alle Pruefungen bestanden.")
