"""
Self-Test für merge_collect (ohne Framework: python test/test_merge_collect.py).
Prüft Datumsparsing, Filter, Konflikterkennung und Zeitraum-Default.
"""
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import merge_collect as mc  # noqa: E402

fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    if not ok:
        fails += 1
    print(f"[{'OK ' if ok else 'XX '}] {label}: got={got!r} want={want!r}")


# --- Datum aus Dateiname --------------------------------------------------
cases = {
    "My_Cents_2026-05-06.md": date(2026, 5, 6),       # ISO
    "My_Cents_2026_05_15.md": date(2026, 5, 15),      # ISO mit _
    "My_Cents_04_01_2026.md": date(2026, 1, 4),       # DD_MM_YYYY (europ.)
    "Dirks_Cents_21_03_2026.mp3": date(2026, 3, 21),  # DD_MM_YYYY
    "My_Cents_01_13_2025.md": date(2025, 1, 13),      # Monat>12 -> Tausch
    "260106_004.md": date(2026, 1, 6),                # YYMMDD kompakt
    "260113_020.md": date(2026, 1, 13),               # YYMMDD kompakt
    "DLF-Nova_Notaufnahme-Reform_2026-04-22.mp3": date(2026, 4, 22),
    "My_Cents_2026-05-26_teil-2.md": date(2026, 5, 26),  # Suffix
    "Iran_Revolution_2026_52th_02.md": None,          # 52 kein Monat
    "Alltag_Textvorlage.md": None,                    # kein Datum
    "Aufnahme #179.md": None,                         # Nummer, kein Datum
}
for name, want in cases.items():
    check(f"parse {name}", mc.parse_date_from_filename(name), want)

# --- Header / Quelle aus Inhalt ------------------------------------------
md = ("# Aufnahme #42\n- Datei: My_Cents_2026-05-06.mp3\n"
      "- Datum: 2026-05-06\n- Status: Rohtranskript\n\nText...")
check("header_date", mc.extract_header_date(md), date(2026, 5, 6))
check("source_audio", mc.extract_source_audio_name(md), "My_Cents_2026-05-06.mp3")
check("source_audio (Transkript-Titel)",
      mc.extract_source_audio_name("# Transkript: 260106_004.mp3\n\nText"),
      "260106_004.mp3")

# --- Wildcard-Filter ------------------------------------------------------
check("My*Cent* trifft My_Cents", mc.matches_patterns("My_Cents_2026-05-06.md", ["My*Cent*"]), True)
check("My*Cent* trifft Dirks nicht", mc.matches_patterns("Dirks_Cents_21_03_2026.md", ["My*Cent*"]), False)
check("Teilstring 'Cent' (auto *Cent*)", mc.matches_patterns("Dirks_Cents_21_03_2026.md", ["Cent"]), True)
check("case-insensitive", mc.matches_patterns("MY_CENTS.md", ["my*cent*"]), True)
check("mehrere Muster (; OR)", mc.matches_patterns("DLF-Nova_x.md", ["My*", "DLF*"]), True)
check("split_patterns", mc.split_patterns("My*Cent*; DLF*"), ["My*Cent*", "DLF*"])

# --- Konflikt -------------------------------------------------------------
c = mc.Candidate(path=Path("x.md"), filename_date=date(2026, 5, 6),
                 header_date=date(2026, 5, 7))
check("conflict erkannt", mc._detect_conflict(c), True)
c2 = mc.Candidate(path=Path("x.md"), filename_date=date(2026, 5, 6),
                  header_date=date(2026, 5, 6))
check("kein conflict bei Gleichheit", mc._detect_conflict(c2), False)
c3 = mc.Candidate(path=Path("x.md"), filename_date=date(2026, 5, 6))
check("kein conflict bei nur 1 Datum", mc._detect_conflict(c3), False)
# Audio-mtime ist fragil -> darf keinen Konflikt auslösen, wenn Name == Header.
c5 = mc.Candidate(path=Path("x.md"), filename_date=date(2026, 5, 6),
                  header_date=date(2026, 5, 6), audio_date=date(2026, 5, 20))
check("Audio-mtime loest keinen Konflikt aus", mc._detect_conflict(c5), False)

# --- Filter-Datum-Priorität ----------------------------------------------
c4 = mc.Candidate(path=Path("x.md"), header_date=date(2026, 5, 7),
                  md_mtime=date(2026, 6, 1))
check("Prio Header vor mtime", mc._pick_filter_date(c4), (date(2026, 5, 7), "Header"))

# --- audio_date toleriert str-Ordner (Regression: str vs Path) -----------
check("audio_date toleriert str-Ordner",
      mc.audio_date("x.mp3", ["nicht_existenter_ordner"]), None)

# --- Zeitraum-Default -----------------------------------------------------
check("Juni-Default", mc.default_month_range(date(2026, 6, 15)),
      (date(2026, 6, 1), date(2026, 6, 30)))
check("Februar-Default (28)", mc.default_month_range(date(2026, 2, 10)),
      (date(2026, 2, 1), date(2026, 2, 28)))
check("Dezember-Default", mc.default_month_range(date(2026, 12, 5)),
      (date(2026, 12, 1), date(2026, 12, 31)))

# --- Basisname ------------------------------------------------------------
check("basename ein Monat", mc.default_basename("MyCents", date(2026, 6, 1), date(2026, 6, 30)),
      "Konsolidat_MyCents_2026-06")

# --- End-to-End: Ausgabe-Dateinamen (kein doppeltes Label im Index) -------
with tempfile.TemporaryDirectory() as td:
    tdp = Path(td)
    (tdp / "My_Cents_2026-05-06.md").write_text(
        "# x\n\nText eins zwei drei", encoding="utf-8")
    # Vorheriges Ausgabe-Artefakt: darf NICHT erneut als Quelle auftauchen.
    (tdp / "Konsolidat_MyCents_2026-04.md").write_text("# alt", encoding="utf-8")

    cands = mc.scan_candidates(tdp, "My*Cent*", False,
                               date(2026, 5, 1), date(2026, 5, 31), [])
    # Scan findet genau die eine echte Quelle (Artefakt ausgeschlossen).
    check("scan findet genau 1 Quelle", len(cands), 1)
    check("Artefakt ausgeschlossen", any(c.name.startswith("Konsolidat_") for c in cands), False)

    cons, idx = mc.write_consolidation(
        cands, tdp, "MyCents", date(2026, 5, 1), date(2026, 5, 31),
        "Positiv", "My*Cent*", write_index=True)
    check("Konsolidat-Name", cons.name, "Konsolidat_MyCents_2026-05.md")
    assert idx is not None, "write_index=True muss einen Quellenindex liefern"
    check("Quellenindex-Name (kein doppeltes Label)", idx.name,
          "Quellenindex_MyCents_2026-05.md")

    # Inhalt prüfen: Quelle referenziert UND Rohtext übernommen.
    cons_text = cons.read_text(encoding="utf-8")
    check("Konsolidat referenziert Quelle",
          "My_Cents_2026-05-06.md" in cons_text, True)
    check("Konsolidat enthält Rohtext", "Text eins zwei drei" in cons_text, True)
    check("Quellenindex listet Quelle",
          "My_Cents_2026-05-06.md" in idx.read_text(encoding="utf-8"), True)

print()
if fails:
    print(f"FEHLGESCHLAGEN: {fails} Checks")
    sys.exit(1)
print("ALLE CHECKS OK")
