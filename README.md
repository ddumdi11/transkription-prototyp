# Transkriptions-Prototyp (Diktiergerät → Text)

Dieses kleine Projekt transkribiert Audiodateien (z. B. vom Diktiergerät) über die OpenAI-API.
Die Sprache ist standardmäßig **Deutsch** und die Transkripte werden als **Markdown (.md)** gespeichert.

## Kosten & Modellwahl

Die OpenAI-Transkription wird pro Audiominute abgerechnet (Prepaid-Guthaben – es kann keine
unerwartete Rechnung entstehen). Seit Juni 2026 ist das günstigste Modell der Standard:

| Modell | Preis/Min | Hinweis |
|--------|-----------|---------|
| `gpt-4o-mini-transcribe` | $0.003 | **Standard** – halber Preis von whisper-1, bessere Qualität |
| `gpt-4o-transcribe` | $0.006 | höchste Qualität |
| `whisper-1` | $0.006 | Legacy |

Mit `--model` lässt sich das Modell frei wählen. Es gibt außerdem eine **kostenlose
lokale Engine** (`faster-whisper`, läuft offline auf dem eigenen Rechner) – siehe
Abschnitt [Engine wählen: Cloud vs. Lokal](#engine-wählen-cloud-vs-lokal).

Optional kann das fertige Transkript zusätzlich durch ein **lokales LLM nachkorrigiert**
werden – siehe [LLM-Korrektur (optional, lokal)](#llm-korrektur-optional-lokal).

## Vorbereitung

1. Python 3.10+ installieren.
2. Im Projektordner die Abhängigkeiten installieren:

   ```bash
   pip install -r requirements.txt
   ```

3. Die Datei `.env.example` kopieren und in `.env` umbenennen:

   ```bash
   cp .env.example .env
   ```

4. In `.env` deinen OpenAI-API-Key eintragen.

5. **ffmpeg** muss installiert sein (für Audio-Zusammenfügen und große Dateien).

---

## GUI-Modus (Empfohlen für gelegentliche Nutzung)

Die grafische Oberfläche ist ideal, wenn du das Tool nicht täglich nutzt und nicht alle CLI-Parameter im Kopf haben möchtest.

### Starten

```bash
python gui.py
```

### Funktionen

Die GUI bietet drei Hauptaktionen:

1. **Audio zusammenfügen** - Fügt alle Audio-Dateien aus Unterordnern zusammen
   - Option: "Nach Zusammenfügen verschieben" (verschiebt Quellordner nach `processed/`)

2. **Transkribieren** - Transkribiert alle Audio-Dateien im Input-Ordner
   - Option: "Bereits transkribierte neu verarbeiten" (überschreibt vorhandene Transkripte)

3. **Kompletter Workflow** - Führt beide Schritte nacheinander aus

### Einstellungen in der GUI

- **Input-Ordner**: Ordner mit Audio-Dateien (Standard: `input`)
- **Output-Ordner**: Ordner für Transkripte (Standard: `output`)
- **Kontext-Prompt**: Fachbegriffe für bessere Erkennung
- **Automatische Ersetzungen**: Korrigiert bekannte Erkennungsfehler

---

## CLI-Modus (Für regelmäßige Nutzung / Automatisierung)

### Schnellstart

```bash
# 1. Audio-Dateien aus Unterordnern zusammenfügen (Ordner nach processed/ verschieben)
python join_audio.py input --all-subfolders --move

# 2. Transkribieren
python transcribe.py input --prompt "Fachbegriffe: Diktiergerät, Claude, Claude Code, KI"
```

### Audio-Dateien zusammenfügen (join_audio.py)

Wenn dein Diktiergerät mehrere Dateien pro Aufnahme-Session erstellt (z.B. bei langen Pausen), kannst du diese vor der Transkription zusammenfügen.

**Standard-Komprimierung:** 64kbps Mono (optimal für Sprache, halbiert die Dateigröße).

#### Einzelnen Ordner zusammenfügen

```bash
python join_audio.py input/My_Cents_10_12_2025
```

Ergebnis: `input/My_Cents_10_12_2025.mp3`

#### Alle Unterordner auf einmal verarbeiten

```bash
python join_audio.py input --all-subfolders
```

#### Nach dem Zusammenfügen Ordner verschieben

```bash
python join_audio.py input/My_Cents_10_12_2025 --move
```

Verschiebt den Ordner nach `input/processed/` als Backup.

#### Vorschau (Dry-Run)

```bash
python join_audio.py input --all-subfolders --dry-run
```

Zeigt an, was gemacht würde, ohne Änderungen vorzunehmen.

#### Intelligente Sortierung

Die Dateien werden automatisch sortiert:

1. **Nach Dateiname** (wenn nummeriert: `001.mp3`, `002.mp3`, ...)
2. **Nach Änderungsdatum** (älteste zuerst, wenn Daten konsistent)
3. **Manuelle Eingabe** (bei unklarer Sortierung, z.B. Datums-Ausreißer durch Batterie-Reset)

Bei Problemen mit dem Datum (z.B. eine Datei hat ein völlig anderes Datum) wirst du gefragt:

```
Dateien konnten nicht automatisch sortiert werden:
  [1] recording_a.mp3  (01.01.1970 00:00)
  [2] recording_b.mp3  (10.12.2024 14:30)
  [3] recording_c.mp3  (10.12.2024 14:45)

Gib die gewünschte Reihenfolge ein (z.B. '2,3,1' oder '2 3 1'):
```

#### join_audio.py Optionen

| Option | Beschreibung |
|--------|--------------|
| `--all-subfolders` | Alle Unterordner verarbeiten |
| `--move` | Ordner nach `processed/` verschieben |
| `--delete` | Ordner löschen (statt verschieben) |
| `--dry-run` | Nur anzeigen, was gemacht würde |
| `--auto` | Keine manuelle Bestätigung bei unsicherer Sortierung |
| `--no-compress` | Keine Komprimierung (Standard: 64kbps Mono) |
| `--bitrate` | Bitrate für Komprimierung (z.B. `64k`, `96k`, `128k`) |

### Transkription (transcribe.py)

#### Grundlegende Verwendung

```bash
python transcribe.py input
```

Die fertigen Transkripte findest du in `output/` als `.md`-Dateien.

#### transcribe.py Optionen

| Option | Beschreibung |
|--------|--------------|
| `--provider` | Engine: `openai` (Cloud, Standard) oder `local` (faster-whisper) |
| `--model` | Modell/Größe. Ohne Angabe wählt jeder Provider seinen Default (openai: `gpt-4o-mini-transcribe`, local: `small`) |
| `--language` | Sprachcode (Standard: `de`) |
| `--output-dir` | Ausgabeordner (Standard: `output`) |
| `--suffix` | Dateiendung (Standard: `.md`) |
| `--prompt` | Kontext-Prompt für bessere Erkennung |
| `--no-replacements` | Standard-Ersetzungen deaktivieren |
| `--no-markdown-title` | Markdown-Titel weglassen |
| `--force` | Bereits transkribierte Dateien erneut verarbeiten |
| `--correct` | Optionale LLM-Nachkorrektur über lokales Ollama/LM Studio aktivieren |
| `--correct-backend` | `ollama` (Standard) oder `lmstudio` (setzt die Default-URL) |
| `--correct-model` | Modellname für die Korrektur (Pflicht bei `--correct`) |
| `--correct-base-url` | Backend-URL überschreiben (abweichende Ports/Hosts) |

#### Beispiele

```bash
# Modell ändern
python transcribe.py input --model whisper-1

# Sprachcode anpassen
python transcribe.py input --language de

# Dateiendung ändern
python transcribe.py input --suffix .txt

# Kontext-Prompt für bessere Erkennung
python transcribe.py input --prompt "Fachbegriffe: Diktiergerät, Claude, Claude Code, KI"

# Ausgabeordner anpassen
python transcribe.py input --output-dir meine_transkripte

# Bereits transkribierte Dateien erneut verarbeiten
python transcribe.py input --force
```

---

## Engine wählen: Cloud vs. Lokal

Die Transkription kann über zwei Engines laufen:

| Engine | `--provider` | Kosten | Hinweis |
|--------|--------------|--------|---------|
| **OpenAI (Cloud)** | `openai` (Standard) | ab $0.003/Min | beste Qualität, benötigt API-Key + Internet |
| **Lokal (faster-whisper)** | `local` | $0 | läuft offline, kein API-Key, kein 25-MB-Limit/Splitting |

```bash
# Cloud (Standard)
python transcribe.py input

# Lokal mit Standard-Modell (small)
python transcribe.py input --provider local

# Lokal mit größerem Modell (genauer, langsamer)
python transcribe.py input --provider local --model medium
```

**Lokale Engine installieren** (optionale, schwergewichtige Abhängigkeit – reine
Cloud-Nutzer brauchen das nicht):

```bash
pip install -r requirements-local.txt
```

Lokale Modellgrößen: `tiny`, `base`, `small` (Default), `medium`, `large-v3`. Beim
ersten Lauf lädt faster-whisper die Modellgewichte herunter (z. B. `small` ≈ 460 MB).
Für deutsches Diktat liefert `small` brauchbare, aber nicht fehlerfreie Ergebnisse;
`medium`/`large-v3` sind genauer, aber langsamer. Höchste Genauigkeit bietet die
Cloud (`--model gpt-4o-transcribe`).

In der GUI wählst du Engine und Modell über die Dropdowns im Bereich **„Engine"**.

---

## LLM-Korrektur (optional, lokal)

Nach der Transkription kann das Transkript zusätzlich durch ein **lokales LLM**
nachkorrigiert werden. Im Gegensatz zu den statischen Wort-Ersetzungen erkennt ein
LLM Fehler im Kontext (z. B. Zeichensetzung, Groß-/Kleinschreibung, falsch erkannte
Wörter). Der Schritt ist **standardmäßig aus** und greift nur mit `--correct`.

> **Wichtig:** Bedeutungsverändernde ASR-Fehler (z. B. „nicht" → „nett") kann ein
> Text-Korrektor prinzipiell nicht zuverlässig reparieren, weil die Information nur
> im Audio steckt. Die LLM-Korrektur glättet Kosmetik und offensichtlichen Wortschrott
> – der eigentliche Qualitätshebel ist die Wahl der Transkriptions-Engine/des Modells.

### Voraussetzung: lokaler LLM-Server

Du brauchst einen laufenden, OpenAI-kompatiblen LLM-Server. **Keine neue pip-Abhängigkeit**
– die vorhandene `openai`-Bibliothek wird wiederverwendet.

| Backend | Default-URL | Vorbereitung |
|---------|-------------|--------------|
| [Ollama](https://ollama.com) | `http://localhost:11434/v1` | Modell vorher `ollama pull <modell>` |
| [LM Studio](https://lmstudio.ai) | `http://localhost:1234/v1` | Modell im Server-Tab laden |

### Verwendung

```bash
# Korrektur über Ollama mit einem geladenen Modell
python transcribe.py input --correct --correct-model llama3.1:8b

# Über LM Studio
python transcribe.py input --correct --correct-backend lmstudio --correct-model <modell>

# Abweichender Port/Host
python transcribe.py input --correct --correct-model <modell> --correct-base-url http://localhost:11434/v1
```

Alternativ in der `.env` konfigurieren (CLI-Argumente haben Vorrang):

```bash
CORRECTION_BACKEND=ollama
CORRECTION_MODEL=llama3.1:8b
# optional:
CORRECTION_BASE_URL=http://localhost:11434/v1
```

In der GUI gibt es dafür den Bereich **„LLM-Korrektur (lokal, optional)"** mit Checkbox,
Backend-Dropdown und Modell-Feld.

### Modell-Empfehlung & Robustheit

- **Modellwahl entscheidet die Qualität.** Ein fähiges Instruct-Modell (Richtwert ≥ 7–8B,
  z. B. `llama3.1:8b`) ist deutlich besser als ein kleines 3B-Modell, das selbst Fehler
  einbaut. Kleinere Modelle paraphrasieren eher – vor dem Dauereinsatz an echtem Diktat prüfen.
- **Ein Transkript geht nie verloren.** Schlägt die Korrektur fehl (Server aus, Timeout,
  Modell nicht geladen, leere Antwort), wird das unkorrigierte Transkript gespeichert und
  die Verarbeitung läuft weiter. Verdächtig kurze Antworten (< 50 % der Originallänge)
  werden verworfen, um versehentliches Zusammenfassen abzufangen.
- **Lange Diktate** werden automatisch an Absatz-/Satzgrenzen in Abschnitte (~6 000 Zeichen)
  zerlegt, einzeln korrigiert und wieder zusammengefügt.
- **Datenschutz:** Die Korrektur läuft lokal – der Text verlässt den Rechner nicht.

---

## Intelligente Verarbeitung

### Überspringen bereits transkribierter Dateien

Standardmäßig werden Dateien übersprungen, für die bereits ein Transkript existiert. So werden bei erneutem Ausführen nur neue Dateien verarbeitet.

Mit `--force` (CLI) oder der Checkbox in der GUI können alle Dateien neu transkribiert werden.

### Automatisches Aufteilen großer Dateien

Dateien über 25 MB (Whisper API Limit) werden automatisch in Teile aufgeteilt, einzeln transkribiert und das Ergebnis zusammengefügt.

---

## Verbesserung der Erkennungsqualität

Die Transkription nutzt zwei Mechanismen zur Verbesserung der Qualität:

### 1. Kontext-Prompt (während der Transkription)

Mit dem `--prompt` Parameter (CLI) oder dem Prompt-Feld (GUI) kannst du Whisper Kontext geben:

```bash
python transcribe.py input --prompt "Fachbegriffe: Diktiergerät, Claude Code, AI, Transkription"
```

Dies hilft besonders bei:

- Eigennamen
- Fachbegriffen
- Ungewöhnlichen Wörtern
- Branchenspezifischer Terminologie

### 2. Automatische Ersetzungen (nach der Transkription)

Das Skript korrigiert automatisch bekannte Erkennungsfehler:

- `Deklärgerät` → `Diktiergerät`
- `Cloud` → `Claude`
- `Cloud Code` → `Claude Code`
- `Cloud AI` → `Claude AI`

Du kannst die Ersetzungen in der Datei `transcribe.py` (Zeilen 28-33) anpassen oder mit `--no-replacements` deaktivieren.

### Eigene Ersetzungen definieren

Siehe `replacements.example.py` für eine Vorlage zum Definieren eigener Ersetzungsregeln.

---

## Empfohlene Verwendung

Für beste Ergebnisse kombiniere beide Mechanismen:

**GUI:**
1. Starte `python gui.py`
2. Trage deine Fachbegriffe im Prompt-Feld ein
3. Klicke auf "Kompletter Workflow"

**CLI:**
```bash
python transcribe.py input --prompt "Fachbegriffe: Diktiergerät, Claude, Claude Code, KI, Transkription"
```

Viel Spaß beim Diktieren und Transkribieren!
