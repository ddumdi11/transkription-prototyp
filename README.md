# Transkriptions-Prototyp (Diktiergerät → Text)

Dieses kleine Projekt transkribiert Audiodateien (z. B. vom Diktiergerät) über die OpenAI-API.
Die Sprache ist standardmäßig **Deutsch** und die Transkripte werden als **Markdown (.md)** gespeichert.

## Kosten & Modellwahl

Die OpenAI-Transkription wird pro Audiominute abgerechnet (Prepaid-Guthaben – es kann keine
unerwartete Rechnung entstehen). Mit `--model` lässt sich das Modell frei wählen; es gibt
außerdem eine **kostenlose lokale Engine** (`faster-whisper`, läuft offline auf dem eigenen
Rechner) – siehe [Engine wählen: Cloud vs. Lokal](#engine-wählen-cloud-vs-lokal). Optional
kann das fertige Transkript zusätzlich durch ein **lokales LLM nachkorrigiert** werden –
siehe [LLM-Korrektur (optional, lokal)](#llm-korrektur-optional-lokal).

## Welches Modell? (Stand: Juni 2026, eigene Tests)

Kurzfassung: **`gpt-4o-mini-transcribe` ist der günstige Default für den Alltag** —
für lange/technische Diktate gezielt `gpt-4o-transcribe` (Details in der Tabelle).

| Modell | Preis/Min | Wann |
|---|---|---|
| `gpt-4o-mini-transcribe` (Default) | $0.003 | Standard für Alltag/einfache Notizen. Bei langen, technischen oder projektrelevanten Diktaten kann es in Wiederholungsschleifen fallen — dann `gpt-4o-transcribe` nutzen. |
| `whisper-1` | $0.006 | Bewährte Alternative. Kein Dauerlimit → unkompliziert bei sehr langen Aufnahmen. |
| `gpt-4o-transcribe` | $0.006 | Empfohlen für projektrelevante/technische/lange Diktate: stabiler, weniger Nacharbeit. Der doppelte Preis ist dort gerechtfertigt. |
| lokal `large-v3` | $0 | Beste Offline-/Datenschutz-Wahl bei ruhigen, klaren Aufnahmen. **Vorsicht:** kann bei Lärm oder Zahlenketten Inhalte halluzinieren → nachprüfen. |
| lokal `medium` / `small` | $0 | Leichtere, schnellere lokale Fallbacks mit geringerer Genauigkeit. |

**Zweistufig:** mini als günstiger Default für den Alltag, `gpt-4o-transcribe` gezielt für anspruchsvolle Diktate — der Mehrpreis spart dort Nacharbeit.

**Hinweise:**

- Lokale Läufe nutzen einen VAD-Filter (Stille-Erkennung), der Wiederholungsartefakte reduziert (abschaltbar).
- Die gpt-4o-Modelle haben ein Limit von ~23 Min pro Datei; lange Aufnahmen werden automatisch geteilt. `whisper-1` und die lokalen Modelle haben kein solches Limit.
- **Der größte Qualitätshebel ist die Aufnahme selbst** — gutes Mikrofon, wenig Wind/Nebengeräusch — mehr als die Modellwahl.
- Der `--prompt` wirkt als Vokabular-Hinweis: trage wiederkehrende Eigennamen und Fachbegriffe ein, das verbessert die Erkennung bei beiden Engines.
- `--hotwords` bleibt beim lokalen Provider als **experimenteller Opt-in**
  verfügbar. In einem reproduzierbaren Vergleich mit einer langen Aufnahme
  führte bereits eine Hotword-Liste allein zu massiven Textauslassungen. Die
  automatische Pipeline und die GUI aktivieren Hotwords deshalb nicht mehr aus
  dem Glossar. Der Kontext-Prompt bleibt der sichere Standard.

### Zentrales Projektglossar

`project_glossary.json` ist die gemeinsame Quelle für projektspezifisches
Vokabular. Ein Eintrag kann gleichzeitig experimentelle Hotwords, sichere
Erkennungsfehler und Routingvarianten definieren. Prompt, Textersetzungen und
Routing lesen diese Datei automatisch; Hotwords werden daraus nicht automatisch
für Volltranskriptionen aktiviert. Ein abweichender Pfad kann über
`AUDIOREC_GLOSSARY=/pfad/zum/glossar.json` gesetzt werden.

Beispiel:

```json
{
  "canonical": "Traktat",
  "hotwords": ["Traktat"],
  "replacements": ["Taktat"]
}
```

`AUDIOREC_HOTWORDS` bleibt ein bewusster experimenteller Override. In der GUI
muss eine gespeicherte Liste zusätzlich ausdrücklich aktiviert werden.

Die wichtigsten technischen Begriffe werden im [gestuften Lernplan für lokale
KI und den digitalen Check](docs/LERNPLAN_LOKALE_KI_UND_DIGITALER_CHECK.md)
als dauerhafte Referenz erklärt und an der bestehenden Pipeline praktisch
wiederholt. Der darin enthaltene Zeitplan dokumentiert die Vorbereitung im
September 2026 und ist nicht mehr aktuell.

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

### Workflow-Modus

Oben im Bereich **Aktionen** wählst du den Workflow-Modus:

- **Einzeln transkribieren (Standard):** Jede Aufnahme wird einzeln transkribiert und
  als eigene Datei mit Metadaten-Kopf gespeichert (Aufnahme-Nr., Datum, Modell, Quelle,
  Status). Aufnahme-Grenzen und Zuordnung bleiben erhalten; nach fehlerfreiem Lauf werden
  die Quell-Unterordner optional nach `processed/` verschoben. Optional (Standard: an)
  entsteht zusätzlich je Quellordner ein **Sammeltranskript auf Textebene**
  (`<Ordnername>.md` aus allen Einzeltranskripten inkl. Metadaten-Köpfen — die
  Einzeltranskripte bleiben erhalten).
- **Vorher zusammenfügen (Sonderfall):** Der alte Ablauf. Nur sinnvoll, wenn Aufnahmen
  bewusst eine einzige zusammenhängende Einheit bilden sollen (z. B. Batch-Kostenoptimierung
  oder längerer Kontext für Satzanschlüsse). Achtung: Im Sammeltranskript können einzelne
  Inhalte untergewichtet oder nicht mehr einer Aufnahme zuordenbar sein (siehe
  `docs/BUG-TRANSCRIBE-001.md`).

### Funktionen

Die GUI bietet drei Hauptaktionen:

1. **Audio zusammenfügen** (nur im Sonderfall-Modus aktiv) - Fügt alle Audio-Dateien aus Unterordnern zusammen
   - Option: "Nach Zusammenfügen verschieben" (verschiebt Quellordner nach `processed/`)

2. **Transkribieren** - Transkribiert alle Audio-Dateien im Input-Ordner
   - Option: "Bereits transkribierte neu verarbeiten" (überschreibt vorhandene Transkripte)
   - Option: "Quellordner nach processed/ verschieben" (nur Einzelmodus)
   - Option: "Sammeltranskript je Ordner erstellen" (nur Einzelmodus)

3. **Kompletter Workflow** - Modusabhängig: im Einzelmodus nur Transkription,
   im Sonderfall-Modus Zusammenfügen + Transkription

### Einstellungen in der GUI

- **Input-Ordner**: Ordner mit Audio-Dateien (Standard: `input`)
- **Output-Ordner**: Ordner für Transkripte (Standard: `output`)
- **Kontext-Prompt**: Fachbegriffe für bessere Erkennung
- **Hotwords**: zusätzliche kommagetrennte Hinweisbegriffe für faster-whisper
- **Automatische Ersetzungen**: Korrigiert bekannte Erkennungsfehler

---

## CLI-Modus (Für regelmäßige Nutzung / Automatisierung)

### Schnellstart (Standard: Einzeltranskription)

```bash
# Jede Aufnahme einzeln transkribieren (Metadaten-Kopf), je Quellordner ein
# Sammeltranskript auf Textebene, Ordner danach nach processed/ verschieben
python transcribe.py input --metadata-header --merge-transcripts --move-processed --prompt "Fachbegriffe: Diktiergerät, Claude, Claude Code, KI"
```

Mit ausdrücklich aktivierten lokalen Hotwords (experimentell; bei langen
Aufnahmen besteht das Risiko von Textauslassungen):

```bash
python transcribe.py input --provider local --model medium \
  --prompt "Fachbegriffe: Diktiergerät, Claude, Claude Code, KI" \
  --hotwords "Traktat, MyOwnCents"
```

Aus `input/MyCents_2026-06-10/` (drei Aufnahmen) entsteht so in `output/`:
`262.md`, `263.md`, `264.md` **und** `MyCents_2026-06-10.md` (Zusammenstellung).

Jedes Transkript beginnt dann mit einem Metadaten-Kopf, z. B.:

```markdown
# Aufnahme #264
- Datei: 264.mp3
- Datum: 2026-06-08
- Modell: gpt-4o-mini-transcribe (openai)
- Quelle: Einzelaufnahme
- Kontext:
- Status: Rohtranskript
```

(`Kontext:` bleibt als Platzhalter leer — für manuelle oder spätere LLM-Einordnung.)

### Sonderfall: Audio vorher zusammenfügen

```bash
# 1. Audio-Dateien aus Unterordnern zusammenfügen (Ordner nach processed/ verschieben)
python join_audio.py input --all-subfolders --move

# 2. Transkribieren
python transcribe.py input --prompt "Fachbegriffe: Diktiergerät, Claude, Claude Code, KI"
```

### Audio-Dateien zusammenfügen (join_audio.py)

**Hinweis:** Seit Juni 2026 ist das Zusammenfügen vor der Transkription der **Sonderfall**,
nicht mehr der Standard (Begründung: `docs/BUG-TRANSCRIBE-001.md` — im Sammeltranskript
gehen Aufnahme-Grenzen und Zuordnung verloren). Nutze es nur, wenn die Aufnahmen bewusst
eine zusammenhängende Einheit bilden sollen.

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

Die fertigen Transkripte findest du in `output/` als `.md`-Dateien. Mit
`--write-segments` schreibt der lokale Provider zusätzlich je Aufnahme eine
maschinenlesbare Datei `<Name>.segments.json` mit Segment-IDs, Start-/Endzeit,
ASR-Rohtext, dem Text nach sicheren Glossarersetzungen und dem SHA-256 des
zugehörigen Markdown-Transkripts.

Die Zeitgrenzen lassen sich anschließend rein lesend auf Plausibilität prüfen:

```bash
.venv/bin/python -m pip install \
  -r requirements.txt -r requirements-local.txt

.venv/bin/python analyze_segment_quality.py \
  "staging/inbox/Aufnahme #1__DRIVE-ID.wav" \
  "staging/transcripts/Aufnahme #1__DRIVE-ID.segments.json"
```

Der Prüfer meldet ungewöhnliche Textdichte oder sehr kurze Textsegmente und
untersucht standardmäßig acht Sekunden Audio davor und danach. FFmpeg streamt
das Fenster nur in den Arbeitsspeicher; Silero-VAD unterscheidet dort
wahrscheinliche Sprache von Stille beziehungsweise sonstigem Geräusch. Weder
Aufnahme noch Transkript, Segmentdatei oder Pipeline-State werden verändert.
`--json` liefert denselben Bericht maschinenlesbar, `--segment-id` nimmt ein
bestimmtes Segment zusätzlich auf und `--text-only` überspringt die
Audioanalyse vollständig. NumPy und Faster-Whisper werden über
`requirements-local.txt` installiert; `--text-only` benötigt sie nicht.

Dabei bezeichnet ein **ASR-Segment** immer den gesamten Modelldatensatz aus
Segmenttext, Startzeit und Endzeit. Das **nominelle Zeitintervall** ist nur der
Audioabschnitt zwischen diesen Zeitangaben. Bei kollabierten oder verschobenen
Grenzen kann der Text richtig sein, obwohl das nominelle Intervall nicht zu ihm
passt.

Für eine kontrollierte Nachprüfung plant der getrennte QA-Helfer deshalb einen
größeren **Prüfausschnitt** um das nominelle Intervall. Ohne `--confirm` zeigt
er ausschließlich den Plan und schreibt nichts:

```bash
.venv/bin/python review_segment_quality.py \
  "staging/inbox/Aufnahme #1__DRIVE-ID.wav" \
  "staging/transcripts/Aufnahme #1__DRIVE-ID.segments.json"
```

Erst die Bestätigung erzeugt verlustfrei kodierte FLAC-Ausschnitte mit
standardmäßig zwölf Sekunden Kontext davor und danach sowie JSON-Metadaten
unter `staging/quality-review/`:

```bash
.venv/bin/python review_segment_quality.py AUDIO SEGMENTE \
  --confirm
```

Mit `--confirm --transcribe-local` wird das Modell einmal geladen und jeder
erzeugte Ausschnitt zusätzlich neu transkribiert. Dieser unabhängige Kontrolllauf
verwendet standardmäßig bewusst weder Pipeline-Prompt noch Hotwords, weil diese
einen sehr kurzen Ausschnitt übersteuern können. Nur
`--use-pipeline-hints` aktiviert beide ausdrücklich für einen Vergleichslauf.
Mehrere `--input AUDIO SEGMENTE` werden mit nur einer Modellladung als Batch
verarbeitet; unterschiedliche Erkennungseinstellungen erhalten getrennte
Ergebnisdateien.

Wenn der erste Vergleich nur einen Textanfang findet und die nächste
ASR-Segmentgrenze weit entfernt liegt, erweitert
`--extend-to-next-segment` den Ausschnitt kontrolliert bis zu dieser Grenze
plus Kontext. `--max-extended-seconds` begrenzt die Erweiterung ab dem
nominellen Start standardmäßig auf 90 Sekunden. Dadurch lassen sich
zusammengeschobene Zeitgrenzen untersuchen, ohne versehentlich beliebig lange
Aufnahmebereiche auszuschneiden.

Der JSON-Vergleich misst die geordnete Wortabdeckung des ursprünglichen
Segmenttexts. Er ist nur ein Prüfsignal und kein automatischer
Wahrheitsentscheid; niedrige oder teilweise Übereinstimmung bleibt eine
Aufforderung zum Probehören. `--segment-id` kann mehrfach angegeben werden, um
gezielt einzelne Stellen auszuwählen. Es erfolgt kein Drive-Upload und keine
Veränderung des kanonischen Transkripts.

Wird erst beim Hören eine zusätzliche Auslassung sichtbar, lässt sich das
betroffene Segment unabhängig von den automatischen Schwellen versionsgebunden
registrieren:

```bash
.venv/bin/python quality_reviews.py \
  --register-manual SEGMENTE_JSON \
  --segment-id segment-000002
```

Ein passender automatischer Kandidat wird dabei dauerhaft als manuell gewählt
markiert; eine passende frühere manuelle Entscheidung wird reaktiviert. Die
Segmentdatei muss über ihren eingebetteten Transkripthash zum gegenwärtigen
Transkript passen. Existiert für dieselbe Transkriptversion bereits ein aktueller
Kandidat, werden zusätzlich Segmenttext und -grenzen verglichen und Abweichungen
abgelehnt. Bei der ersten Registrierung gibt es dafür keinen Vergleichsdatensatz;
der ausgegebene Kandidatenhash bindet Text, Grenzen und die spätere Entscheidung
an genau diese Fassung.

Bestätigte Hörentscheidungen lassen sich anschließend schreibgeschützt zu
einem Reparaturplan zusammenführen:

```bash
.venv/bin/python plan_transcript_repairs.py
.venv/bin/python plan_transcript_repairs.py --drive-id DRIVE_ID --json
```

Neue Entscheidungen binden Kandidatenhash und Segment an den SHA-256 des
Transkripts zum Entscheidungszeitpunkt. Der Planer vergleicht ihn mit der
aktuellen lokalen Datei: `MATCHED` ist versionssicher, `MISMATCH` verweist auf
eine inzwischen geänderte Datei und ältere Entscheidungen ohne Hash erscheinen
als `UNVERIFIED`. Reine Grenzfehler werden als `NO_TEXT_CHANGE` ausgewiesen.
Wortfehler, Auslassungen, Halluzinationen und sonstige bestätigte Textfehler
bleiben bis zur ausdrücklichen Kuratierungsaktion `CURATION_REQUIRED`. Erst
`--curate-replacement` macht einen Ersatz als
`CURATED_REPLACEMENT_AVAILABLE` verfügbar; ein bei der Hörentscheidung
gespeicherter Belegtext genügt dafür bewusst nicht und bleibt bei der späteren
Kuratierung unverändert erhalten. Belegtext und segmentgenauer Ersatztext
werden getrennt gespeichert. Bereits bestätigte
Entscheidungen lassen sich getrennt und versionsgebunden kuratieren:

```bash
.venv/bin/python quality_reviews.py \
  --drive-id DRIVE_ID --segment-id SEGMENT_ID \
  --candidate-hash KANDIDATEN_HASH \
  --curate-replacement "Vollständiger bestätigter Text"

.venv/bin/python quality_reviews.py \
  --drive-id DRIVE_ID --segment-id SEGMENT_ID \
  --candidate-hash KANDIDATEN_HASH --curate-delete
```

Der Reparaturplan verwendet Schema 3 und bindet seinen vollständigen Inhalt
über `plan_sha256`. Vor einer späteren Vorschauerzeugung lässt sich ein
eingefrorener Plan vollständig schreibfrei gegen den aktuellen Zustand prüfen:

```bash
.venv/bin/python plan_transcript_repairs.py \
  --drive-id DRIVE_ID --json > /tmp/reparaturplan.json

.venv/bin/python build_transcript_previews.py \
  --plan /tmp/reparaturplan.json \
  --expect-plan-sha256 PLAN_SHA256 \
  --check --json
```

`--check` akzeptiert nur einen unveränderten Schema-3-Plan, ausschließlich
versionsgebundene `MATCHED`-Reviews und einen aktuellen Datenbankzustand, der
denselben Plan reproduziert. Ein einziger `CURATION_REQUIRED`, `MISMATCH` oder
`UNVERIFIED`-Eintrag sperrt den gesamten Lauf. Dieser erste Sicherheitsbaustein
schreibt ausdrücklich noch keine Vorschaufassung.

`--curate-delete` ist eine ausdrückliche Löschentscheidung für eine bestätigte
Halluzination und wird im Plan als `CURATED_DELETION_AVAILABLE` geführt. Eine
identische Wiederholung ist erlaubt; eine abweichende Zweitkuratierung oder eine
inzwischen veränderte Transkriptversion wird abgelehnt. Beide Operationen
ändern ausschließlich den lokalen QA-State, niemals Transkript oder Drive.
Der Quellhash wird schon
bei der Kandidatenregistrierung gebunden, vor der Entscheidung erneut geprüft
und für die spätere Planprüfung gespeichert. Der Befehl öffnet die
Pipeline-Datenbank ausschließlich lesend und verändert weder Transkript noch
Pipeline-State oder Drive.

Bestätigte Altfälle aus der Zeit vor dieser Versionsbindung lassen sich
zunächst rein lesend prüfen:

```bash
.venv/bin/python bind_legacy_quality_reviews.py
```

Nur wenn aktuelles Transkript, veröffentlichter Hash, Segmentkandidat und
Zeitfolge widerspruchsfrei zusammenpassen, erscheint `ELIGIBLE`. Eine Bindung
erfordert anschließend den exakt angezeigten Plan-Hash:

```bash
.venv/bin/python bind_legacy_quality_reviews.py \
  --confirm-plan-sha256 SHA256_AUS_DEM_DRY_RUN
```

Die bestätigte Ausführung aktualisiert atomar ausschließlich die
Versionsbindung und den versionsgebundenen Kandidatenfingerabdruck in der
lokalen QA-Datenbank. Transkripte, Segmentdateien und Drive bleiben unverändert.

Ältere Segmentdateien besitzen teilweise noch keinen eingebetteten
`transcript_sha256` und bleiben deshalb bei Archiv- und QA-Prüfungen
vorsorglich gesperrt. Der Legacy-Metadaten-Binder plant ihre Nachbindung
zunächst vollständig lesend:

```bash
.venv/bin/python bind_legacy_segment_metadata.py
.venv/bin/python bind_legacy_segment_metadata.py --eligible-only --json
```

`ELIGIBLE` setzt voraus, dass Drive-ID, veröffentlichter lokaler Hash,
Dateiname und der aus allen Segmenttexten rekonstruierte Transkriptkörper
übereinstimmen. LLM-korrigierte oder abweichende Transkripte bleiben auf
`HOLD`. `--eligible-only` nimmt ausschließlich vollständig belegte Fälle in
den hash-gebundenen Plan auf; die Anzahl beobachteter HOLD-Fälle bleibt Teil
des Plan-Hashes. Erst die Wiederholung mit exakt diesem Hash schreibt:

```bash
.venv/bin/python bind_legacy_segment_metadata.py \
  --eligible-only \
  --confirm-plan-sha256 SHA256_AUS_DEM_DRY_RUN
```

Der bestätigte Lauf ergänzt ausschließlich den Transkripthash in jeder
unveränderten Segmentdatei. Die Installation erfolgt pro Datei atomar und
dauerhaft, der ursprüngliche Datei-Zeitstempel bleibt erhalten. Ein inzwischen
verändertes Transkript, eine veränderte Segmentdatei oder ein anderer
beobachteter Status entwertet den Plan. Weder Transkript, Pipeline-Datenbank
noch Drive werden geändert.

Ein ausdrücklich bestätigtes Korrekturbeispiel kann anschließend zunächst als
Dry-Run geplant werden:

```bash
.venv/bin/python create_correction_sample.py \
  "staging/inbox/Aufnahme #1__DRIVE-ID.wav" \
  "staging/transcripts/Aufnahme #1__DRIVE-ID.segments.json" \
  --term "Traktat"
```

Erst derselbe Aufruf mit `--confirm` schreibt den Audioausschnitt ohne
Neukodierung sowie seine JSON-Metadaten nach `staging/training-samples/`. Kommt
der Begriff in mehreren Segmenten vor, muss zusätzlich `--segment-id` angegeben
werden. Der FFmpeg-Timeout wird aus der Segmentdauer abgeleitet und kann für
langsame Systeme mit `--timeout-seconds` überschrieben werden. Das Werkzeug
lädt nichts auf Drive und verändert weder das kanonische Transkript noch die
Aufnahme.

Veröffentlichte Aufnahmen lassen sich außerdem schreibgeschützt zu zeitlich
zusammengehörigen Sitzungen gruppieren:

```bash
.venv/bin/python plan_transcript_sessions.py --date 2026-09-19 --json
```

Der Planer verwendet Drive-Zeit und Segmentdauer, standardmäßig mit 90 Minuten
maximaler Ruhezeit. `--break-before DRIVE_ID` und
`--join-with-previous DRIVE_ID` erlauben nachvollziehbare manuelle Ausnahmen.
Details stehen in [docs/INBOX_WATCHER.md](docs/INBOX_WATCHER.md).

Auf dieser Gruppierung baut der segmentgenaue Projekt-Routingplan auf:

```bash
.venv/bin/python plan_session_routing.py --date 2026-09-19 --json
```

Er verbindet sitzungsweite Standard- und Kontextprojekte mit konkreten
Projekt- und Themenbelegen je Segment. Nicht durch eine Inhaltsregel erfasste
Segmente werden ausdrücklich ausgewiesen. Auch dieser Schritt ist vollständig
lesend und erzeugt noch keine Projektkopien oder Manifeste. Nach manueller
Prüfung können eine oder mehrere angezeigte Sitzungen ausdrücklich lokal
bestätigt werden:

```bash
.venv/bin/python plan_session_routing.py --date 2026-09-19 \
  --confirm-session SESSION_ID
```

Das atomar geschriebene Manifest unter `staging/routing-manifests/` enthält den
vollständigen Plan und dessen SHA256. Identische Wiederholungen sind idempotent;
abweichende Inhalte unter derselben Sitzungs-ID werden nicht überschrieben.
Drive und Pipeline-Status bleiben auch dabei unverändert.

Ein aus Atlas exportierter Projektkatalog kann vor seiner späteren Verwendung
schreibgeschützt gegen den versionierten Schnittstellenvertrag geprüft werden:

```bash
.venv/bin/python project_catalog.py atlas-project-catalog.v1.json
```

Der Validator kontrolliert Struktur, stabile Vorhaben-Slugs, Beziehungen,
Routingbegriffe und den kanonischen Kataloghash, schreibt aber keine Datei und
ändert weder Pipeline-Status noch Drive.

Ein ausdrücklich bestätigtes Sitzungsmanifest kann anschließend zunächst als
lokales, verifiziertes Atlas-Lieferpaket geplant werden:

```bash
.venv/bin/python export_transcript_delivery.py \
  --routing-manifest staging/routing-manifests/routing__SESSION_ID.json \
  --catalog /pfad/zu/atlas-project-catalog.v1.json
```

Erst `--confirm` erzeugt das Paket atomar unter
`staging/project-deliveries/`. Es enthält verifizierte Kopien der bereits
kanonisch veröffentlichten Transkripte und zuletzt `delivery.json`. Der
Exporter lädt noch nichts auf Drive und verändert weder Pipeline-State noch
kanonische Transkripte. Das zugehörige Vertragsschema liegt unter
`schemas/z-system-transcript-delivery.schema.v1.json`.

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
| `--metadata-header` | Metadaten-Kopf je Transkript (Aufnahme-Nr., Datum, Modell, Quelle, Status) — für den Einzeldatei-Workflow |
| `--write-segments` | Beim lokalen Provider zusätzlich `<Name>.segments.json` mit Zeitsegmenten schreiben |
| `--source-id` | Optionale stabile Quell-ID in der Segmentdatei; die Inbox-Pipeline setzt hier die Drive-ID |
| `--merge-transcripts` | Je Quell-Unterordner zusätzlich ein Sammeltranskript (`<Ordnername>.md`) auf Textebene; Einzeltranskripte bleiben erhalten |
| `--move-processed` | Quell-Unterordner nach fehlerfreiem Lauf nach `processed/` verschieben |
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
Welche Größe wann sinnvoll ist (und wie die lokalen Modelle gegen die Cloud abschneiden),
steht im Abschnitt [Welches Modell?](#welches-modell-stand-juni-2026-eigene-tests).

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

Zu große oder zu lange Dateien werden automatisch in Teile aufgeteilt, einzeln transkribiert und das Ergebnis zusammengefügt (temporäre Teildateien werden danach aufgeräumt). Was die Teilung auslöst, hängt von Provider und Modell ab:

- **`whisper-1`:** nur das 25-MB-Größenlimit der API.
- **`gpt-4o-(mini-)transcribe`:** zusätzlich ein Dauerlimit von ~23 Min pro Anfrage — lange Aufnahmen werden entsprechend nach Dauer geteilt (die bindende Grenze entscheidet).
- **Lokal (`faster-whisper`):** kein Größen- oder Dauerlimit, also kein Splitting.

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
- `Audio Rack` → `AudioRec`
- `Taktat` → `Traktat`
- `Job-Kühe` → `Job Queue`

Mehrdeutige Wörter wie `Cloud` werden bewusst nicht automatisch ersetzt.
`Claude`, `Claude Code` und `Claude AI` bleiben stattdessen Hotwords für die
Spracherkennung.

Du kannst die zentralen Fachbegriffe und sicheren Ersetzungen in
`project_glossary.json` anpassen oder Ersetzungen mit `--no-replacements`
deaktivieren.

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

---

## AudioRec-Smartphone rein lesend inventarisieren

`inventory_audiorec_phone.py` erfasst einen über USB/MTP eingebundenen
AudioRec-Aufnahmeordner, ohne dort Dateien anzulegen, zu verändern oder zu
entfernen. Ein erster Prüfscan schreibt mit `--check` auch lokal weder SQLite
noch Bericht:

```bash
.venv/bin/python inventory_audiorec_phone.py \
  --source "/run/user/1000/gvfs/mtp:host=GERAETE_ID/Interner Speicher/Android/data/com.audioRec/files/recordings" \
  --device-id audiorec-a53 \
  --check
```

Jedes Smartphone benötigt eine eigene, dauerhaft stabile `--device-id`. Ohne
diese Option verwendet der Helfer `audiorec-android-primary`.

Erfasst werden ausschließlich WAV-Dateien direkt im angegebenen Quellordner.
Unterordner und andere Dateiformate werden übersprungen; der Bericht ist daher
kein vollständiges Verzeichnisinventar des Ordners.

Der normale Lauf führt den getrennten lokalen Zustand unter
`.inbox-watcher/audiorec-inventory.sqlite3` und installiert den letzten
JSON-Bericht atomar unter
`staging/audiorec-inventory/latest-report.json`. Die voreingestellte
Grenze `coverage_from` ist `2025-12-01T00:00:00+01:00`. Sie bestätigt nicht,
dass seit diesem Zeitpunkt jede jemals vorhandene Aufnahme vollständig erfasst
ist.
AudioRec-Nummern und optionale Endziffern werden nur als nicht eindeutige
Hinweise erfasst; die vorläufige Identität bindet zusätzlich Gerät, Epoche,
Pfad, Größe und Änderungszeit. Beliebige Unicode-Namen bleiben inventarisierbar
und werden bei abweichendem Schema als Anomalie gemeldet.

v0.1 berechnet noch keine vollständigen Audio-Hashes, gleicht nicht mit Drive
oder Pipeline ab und erteilt keine Löschfreigaben. `--inspect-audio` öffnet
optional die WAV-Köpfe; der schnelle Standardscan vermeidet diese vielen
MTP-Zugriffe. Details stehen in
[`docs/INBOX_WATCHER.md`](docs/INBOX_WATCHER.md#audiorec-smartphone-inventur-v01).

Der ebenfalls rein lesende Sicherungsplaner gleicht einen gespeicherten
Inventarstand mit den historischen Pipeline-Belegen ab:

```bash
.venv/bin/python plan_audiorec_backup.py --device-id audiorec-a53
```

Ein eindeutiger Treffer über Dateiname und Größe liefert nur einen Kandidaten
für den bekannten Pipeline-Hash (`PHONE_HASH_REQUIRED`). Erst ein späterer
verifizierter Smartphone-Hash kann `CONTENT_HASH_MATCH` ergeben; auch dieser
Status verlangt anschließend noch eine aktuelle Remote-Prüfung. Der Planer
fragt weder Drive noch das Smartphone ab, schreibt keine Datenbank und bestätigt
keine Sicherung. Einzelne Nummern lassen sich wiederholbar mit
`--recording-number` auswählen; `--json` gibt den vollständigen Plan aus.

---

## Projektübergreifender Entwurf

Das zunächst manuelle, datensparsame Messschema zur vergleichbaren Auswahl von
Modellen und Agenten ist in
[`docs/MODELL_AGENTEN_AUSWAHL_MESSSCHEMA.md`](docs/MODELL_AGENTEN_AUSWAHL_MESSSCHEMA.md)
dokumentiert. Es ist noch nicht produktiv an die Pipeline oder Atlas gekoppelt.
