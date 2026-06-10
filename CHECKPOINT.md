# Projekt-Checkpoint

**Datum:** 10. Juni 2026 (ursprünglich: 7. Dezember 2024)
**Projekt:** Transkriptions-Prototyp (Diktiergerät → Text)

## Update 10.06.2026 – Einzeldatei-Workflow als neuer Standard (BUG-TRANSCRIBE-001)

Außerplanmäßige Einheit nach Bug-Report `docs/BUG-TRANSCRIBE-001.md`
(Workflow-Designfehler: Audio-Zusammenfügen *vor* der Transkription verliert
Aufnahme-Grenzen/Metadaten; Inhalt aus #264 war im Sammeltranskript #262–#264
nicht auffindbar).

- ✅ **`transcribe.py`:** `--metadata-header` (Metadaten-Kopf je Transkript:
  Aufnahme-Nr. aus Dateiname, Datum aus Dateidatum, Modell, Quelle,
  Kontext-Platzhalter, Status) und `--move-processed` (Quell-Unterordner nach
  fehlerfreiem Lauf nach `processed/`; defensiv — bei Fehlern bleibt der Ordner
  liegen). Ohne Flags unverändertes Verhalten (CLI-kompatibel).
- ✅ **GUI:** Workflow-Modus-Schalter — „Einzeln transkribieren" (neuer
  Standard) vs. „Vorher zusammenfügen" (Sonderfall, alter Ablauf). Im
  Einzelmodus: Join-Button deaktiviert, „Kompletter Workflow" = nur
  Transkription mit Metadaten-Kopf + optionalem `processed/`-Verschieben.
  Modus wird in `gui_settings.json` persistiert.
- ✅ **Sammeltranskript auf Textebene:** `--merge-transcripts` erstellt je
  Quell-Unterordner zusätzlich `<Ordnername>.md` aus allen Einzeltranskripten
  (inkl. Metadaten-Köpfen, sortiert nach Aufnahme-Nr.; Einzeltranskripte
  bleiben erhalten; nur bei fehlerfreiem Ordner). GUI-Checkbox
  „Sammeltranskript je Ordner erstellen" (Einzelmodus, Standard: an).
- ✅ **Verifiziert:** Integrationstest mit Stub-Provider (Metadaten-Kopf,
  Sammeltranskript inkl. Reihenfolge, Verschiebe-Logik, Root-Dateien bleiben
  liegen, altes Verhalten ohne Flags unverändert) — 22/22 Prüfungen grün.
- ✅ **A/B-Vergleich am echten Tagesmaterial** (`docs/VERGLEICH_WORKFLOW_2026-06-10.md`):
  alter Weg verlor ~19 % Inhalt, alle Verluste an Split-Grenzen der gejointen
  Datei. Nebenbefund: auch der neue Weg verliert ~45 Wörter an internen
  Split-Grenzen bei Aufnahmen >23 Min.
- 📋 **Offen (nächste Einheit, priorisiert):** **Überlapp-Splitting** —
  Teildateien mit 10–15 s Überlappung schneiden und Nahtstellen textseitig
  deduplizieren (alternativ Schnitt an Stille-Grenzen). Behebt den
  Restverlust an Split-Grenzen. Danach: Tages-*Analyse* und *Übergabenotiz*
  (LLM-gestützt) auf Basis des Sammeltranskripts.

## Update 01.06.2026 – Kostenoptimierung & Architektur

- ✅ **Default-Modell gewechselt:** `whisper-1` → `gpt-4o-mini-transcribe`
  (halber Preis: $0.003 statt $0.006/Min, bessere Qualität, voll kompatibel).
  `whisper-1` und `gpt-4o-transcribe` bleiben über `--model` wählbar.
- ✅ **`.gitattributes`** hinzugefügt: normalisiert Zeilenenden (LF im Repo) und
  beseitigt das CRLF↔LF-Diff-Rauschen.
- ✅ **README** um Abschnitt „Kosten & Modellwahl" ergänzt.
- ✅ **Landing Page** (`docs/index.html`) – GitHub-Pages-fähig aus `/docs`.
- 📋 **Implementierungs-Plan** (`docs/IMPLEMENTATION_PLAN.md`) für die nächste
  Iteration: Provider-Abstraktion (OpenAI + **lokal via faster-whisper**),
  GUI-Modellauswahl. Umsetzung durch Claude Code (Kurt).
  Hinweis: Lokale Transkription = `faster-whisper`, **nicht** Ollama/LM Studio
  (das sind LLM-Server ohne saubere ASR; nur als optionaler Korrekturschritt vorgesehen).

## Update 01.06.2026 – Provider-Abstraktion, lokale Engine & LLM-Korrektur

Umgesetzt durch Claude Code (Kurt), in getrennten Commits:

- ✅ **Provider-Abstraktion** (`providers/`): Transkriptions-Engine austauschbar
  (Commit `6dfd947`). `--provider {openai,local}`; `--model=None` → jeder Provider
  wählt seinen Default (openai: `gpt-4o-mini-transcribe`, local: `small`).
- ✅ **Lokale Engine** `faster-whisper` (`providers/local_provider.py`, Commit
  `66ee3e4`): kostenlos, offline, kein 25-MB-Splitting; optionale Abhängigkeit
  (`requirements-local.txt`), defensiver Import. GUI mit Engine-/Modell-Dropdown,
  Einstellungen in `gui_settings.json`.
  - Qualität (echter Lauf): `small` für dt. Diktat brauchbar, aber nicht fehlerfrei;
    `medium`/`large-v3` genauer; höchste Genauigkeit über Cloud `gpt-4o-transcribe`.
- ✅ **Optionale LLM-Korrektur** (`correction/`, Commit `dd22d5b` + Chunking/GUI/Doku):
  Nachkorrektur über lokales Ollama/LM Studio (OpenAI-kompatibel, keine neue
  Abhängigkeit). `--correct`/`--correct-backend`/`--correct-model`/`--correct-base-url`,
  `.env` `CORRECTION_*` mit CLI-Vorrang. **Default aus.** Defensiv: jeder Fehler →
  unkorrigiertes Transkript behalten; Längen-Schutz (< 50 %); Chunking langer Texte
  an Absatz-/Satzgrenzen (~6 000 Zeichen). GUI-Checkbox + Backend/Modell + Persistenz.
- 📋 Pläne: `docs/IMPLEMENTATION_PLAN.md` (Provider, abgeschlossen),
  `docs/LLM_CORRECTION_PLAN.md` (Korrektur), `docs/ASR_QUALITY_SPIKE.md`
  (Mess-Protokoll für den eigentlichen Qualitätshebel; audio-nativer Gemma/llama.cpp-
  Pfad bewusst geparkt).

## Aktueller Stand

Das Projekt ist **funktionsfähig und produktionsbereit**. Alle Kernfunktionen sind implementiert und getestet.

## Implementierte Features

### Basis-Funktionalität

- ✅ Transkription von Audiodateien über OpenAI Whisper API
- ✅ Unterstützung mehrerer Audioformate: `.mp3`, `.wav`, `.m4a`, `.flac`, `.ogg`, `.webm`
- ✅ Batch-Verarbeitung von Ordnern mit mehreren Audiodateien
- ✅ Ausgabe als Markdown-Dateien (`.md`) mit optionalem Titel
- ✅ Konfiguration über `.env` Datei (OpenAI API-Key)

### GUI-Modus (neu: 19.12.2024)

- ✅ **gui.py**: Grafische Oberfläche mit tkinter (keine zusätzlichen Abhängigkeiten)
  - Drei Hauptaktionen: Audio zusammenfügen, Transkribieren, Kompletter Workflow
  - Ordner-Auswahl über Dialog
  - Checkbox-Optionen für häufige Parameter (--move, --force, --no-replacements)
  - Kontext-Prompt-Eingabefeld
  - Live-Ausgabe der Befehle im Log-Bereich
  - Statusleiste für aktuellen Fortschritt

### Qualitätsverbesserungen

- ✅ **Kontext-Prompt Support**: Möglichkeit, Whisper Kontext für bessere Erkennung zu geben
  - Parameter: `--prompt "Fachbegriffe: ..."`
  - Hilft bei Eigennamen, Fachbegriffen, ungewöhnlichen Wörtern

- ✅ **Automatische Ersetzungen**: Post-Processing für bekannte Erkennungsfehler
  - `Deklärgerät` → `Diktiergerät`
  - `Cloud` → `Claude`
  - `Cloud Code` → `Claude Code`
  - `Cloud AI` → `Claude AI`
  - Anpassbar in `transcribe.py` (Zeilen 28-33)
  - Optional deaktivierbar mit `--no-replacements`

- ✅ **Beispiel-Konfiguration**: `replacements.example.py` als Vorlage für eigene Ersetzungsregeln

### Audio-Dateien zusammenfügen

- ✅ **join_audio.py**: Skript zum Zusammenfügen mehrerer Audio-Dateien aus Unterordnern
  - Nutzt ffmpeg
  - **Standard: 64kbps Mono** (optimal für Sprache, kleine Dateien)
  - Mono-Konvertierung für halbe Dateigröße bei gleicher Sprachqualität
  - Einzelne Ordner oder alle Unterordner auf einmal verarbeiten
  - **Intelligente Sortierung**:
    - Priorität 1: Dateiname (wenn nummeriert)
    - Priorität 2: Änderungsdatum (älteste zuerst)
    - Priorität 3: Manuelle Eingabe bei Unklarheit
  - **Datums-Ausreißer-Erkennung** (z.B. Batterie-Reset → Datum 1970)
  - **Manuelle Sortierung**: Bei unsicherer Auto-Sortierung wird nachgefragt
  - `--move`: Quellordner nach `input/processed/` verschieben
  - `--delete`: Quellordner löschen
  - `--dry-run`: Vorschau ohne Änderungen
  - `--auto`: Keine Nachfrage bei unsicherer Sortierung
  - `--no-compress`: Keine Komprimierung
  - `--bitrate`: Andere Bitrate wählen (z.B. `64k`, `192k`)

### Intelligente Transkription

- ✅ **Überspringen bereits transkribierter Dateien**
  - Standard: Dateien mit vorhandenem Transkript werden übersprungen
  - `--force`: Alle Dateien neu transkribieren

- ✅ **Automatisches Aufteilen großer/langer Dateien** (provider-/modellabhängig)
  - `whisper-1`: nur 25-MB-Größenlimit der API
  - `gpt-4o-(mini-)transcribe`: zusätzlich Dauerlimit ~23 Min/Anfrage → Dauer-Split
  - lokal (`faster-whisper`): kein Größen-/Dauerlimit → kein Split
  - Teile werden einzeln transkribiert und zusammengefügt; temporäre Dateien aufgeräumt

### CLI-Optionen (transcribe.py)

- `--provider`: Engine `openai` (Standard) oder `local` (faster-whisper)
- `--model`: Modell/Größe; Default providerabhängig (`openai`: `gpt-4o-mini-transcribe`, `local`: `small`)
- `--language`: Sprache festlegen (Standard: `de`)
- `--output-dir`: Ausgabeordner (Standard: `output`)
- `--suffix`: Dateiendung (Standard: `.md`)
- `--prompt`: Kontext-Prompt für bessere Erkennung
- `--no-replacements`: Automatische Ersetzungen deaktivieren
- `--no-markdown-title`: Markdown-Titel weglassen
- `--force`: Bereits transkribierte Dateien erneut verarbeiten

## Dateistruktur

```text
transkription_prototyp/
├── .env                      # API-Konfiguration (nicht in Git)
├── .env.example              # Vorlage für .env
├── README.md                 # Vollständige Dokumentation (CLI + GUI)
├── CHECKPOINT.md             # Dieser Status-Bericht
├── requirements.txt          # Python-Abhängigkeiten
├── transcribe.py             # Transkriptions-Skript (CLI)
├── join_audio.py             # Audio-Dateien zusammenfügen (CLI)
├── gui.py                    # Grafische Oberfläche (NEU)
├── replacements.example.py   # Vorlage für eigene Ersetzungen
├── input/                    # Audiodateien (vom Benutzer erstellt)
│   └── processed/            # Verarbeitete Quellordner (nach --move)
└── output/                   # Generierte Transkripte (automatisch erstellt)
```

### Weitere Verbesserungen

- ✅ **`processed/` Ordner wird ignoriert**: Keine doppelte Transkription der Quelldateien
- ✅ **ffmpeg Komprimierung repariert**: Expliziter Codec (`libmp3lame`) für zuverlässige Komprimierung
- ✅ **Windows-Encoding-Fix**: Alle Unicode-Sonderzeichen (→, ✔, ✖, etc.) durch ASCII ersetzt für Kompatibilität mit Windows cp1252

## Getestet

- ✅ Transkription funktioniert einwandfrei
- ✅ Erkennungsqualität ist gut (einige wenige Wortfehler)
- ✅ Ersetzungen korrigieren bekannte Fehler erfolgreich
- ✅ Audio-Zusammenfügen mit Komprimierung auf 64k Mono
- ✅ Auto-Split bei Dateien >25MB
- ✅ GUI startet und führt Befehle korrekt aus (auch unter Windows ohne Encoding-Fehler)

## Nächste mögliche Schritte (optional)

Falls du das Projekt weiterentwickeln möchtest:

1. **Eigene Ersetzungen erweitern**: Weitere häufige Fehler in `DEFAULT_REPLACEMENTS` aufnehmen
2. **Externe Ersetzungsdatei**: Import aus `replacements.py` ermöglichen
3. **Statistiken**: Zusammenfassung nach Batch-Verarbeitung (Anzahl Dateien, Fehler, etc.)
4. **Zeitstempel-Modus**: Option für Transkription mit Zeitmarken
5. **GUI-Erweiterungen**: Fortschrittsbalken, Einstellungen speichern

## Notizen

- Die Whisper API erlaubt kein direktes Fine-tuning
- Kontext-Prompt + Ersetzungen sind die beste Kombination für Qualität
- API-Key in `.env.example` sollte invalidiert werden (falls echt)
- GUI nutzt tkinter (in Python Standard-Bibliothek enthalten)

## Verwendung

### GUI (empfohlen für gelegentliche Nutzung)

```bash
python gui.py
```

### CLI (empfohlen für regelmäßige Nutzung / Automatisierung)

```bash
# 1. Falls nötig: Audio-Dateien aus Unterordnern zusammenfügen
python join_audio.py input --all-subfolders --move

# 2. Transkribieren
python transcribe.py input --prompt "Fachbegriffe: Diktiergerät, Claude, Claude Code, KI, Transkription"
```

---

**Status:** ✅ Projekt abgeschlossen und einsatzbereit
