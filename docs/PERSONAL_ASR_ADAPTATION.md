# Persönliche ASR-Anpassung

## Aktueller Stand

Die lokale faster-whisper-Engine unterstützt einen Kontext-Prompt und eine
separate Hotword-Liste. Die automatische Pipeline liest sie aus
`AUDIOREC_PROMPT` und `AUDIOREC_HOTWORDS`; die GUI speichert beide Angaben in
ihren Einstellungen.

Die Standardwerte und sicheren Ersetzungen stammen aus
`project_glossary.json`. Routingvarianten können dort demselben kanonischen
Begriff und einem oder mehreren Projekten zugeordnet werden. Damit bleibt das
projektspezifische Vokabular an einer zentralen Stelle prüfbar.

Hotwords sind Hinweise für die Erkennung. Sie sind keine Textersetzungen und
belegen daher allein noch nicht, dass ein Wort falsch erkannt oder korrigiert
wurde.

Die lokale Transkriptionsschnittstelle bewahrt inzwischen die von
faster-whisper gelieferten Segmenttexte und Zeitgrenzen. Mit
`--write-segments` entsteht neben dem Markdown eine Begleitdatei
`<Name>.segments.json`. Sie enthält stabile Segment-IDs, Start-/Endzeit, den
unbearbeiteten ASR-Text und separat den Text nach sicheren
Glossarersetzungen. Die automatische Inbox-Pipeline aktiviert diese Ausgabe
und trägt die eindeutige Drive-ID als `source_id` ein. Sie akzeptiert einen Job
nur mit vorhandener Segmentdatei als abgeschlossen.

## Segmentzeiten rein lesend prüfen

`analyze_segment_quality.py` sucht konservativ nach Segmenten, deren Textmenge
nicht zur angegebenen Dauer passt. Für jeden Treffer wird ein gepolstertes
Audiofenster standardmäßig acht Sekunden vor und nach dem Segment direkt über
FFmpeg in den Arbeitsspeicher gelesen. Der bereits mit Faster-Whisper
installierte Silero-Sprachdetektor bewertet getrennt:

- das Fenster vor dem nominellen Segment,
- das nominelle Segment selbst,
- das Fenster danach.

Damit lässt sich unterscheiden, ob rund um einen unmöglich kurzen Zeitstempel
weiterhin Sprache vorhanden ist, nur Geräusch erkannt wird oder das Umfeld
still ist. Sprache vor und nach einem zu kurzen Segment ist ein Hinweis auf
verrutschte beziehungsweise kollabierte Zeitgrenzen, aber noch kein Beweis für
fehlenden oder halluzinierten Text. Der Prüfer verändert grundsätzlich keine
Datei und führt keine automatische Korrektur durch.

```bash
.venv/bin/python -m pip install \
  -r requirements.txt -r requirements-local.txt

.venv/bin/python analyze_segment_quality.py \
  "staging/inbox/Aufnahme #1__DRIVE-ID.wav" \
  "staging/transcripts/Aufnahme #1__DRIVE-ID.segments.json"
```

Mit `--json` kann der Bericht später von einer Prüfansicht verarbeitet werden.
`--segment-id SEGMENT-ID` nimmt eine Stelle unabhängig von den automatischen
Schwellen auf; `--text-only` prüft ausschließlich die Segmentmetadaten und
benötigt die in `requirements-local.txt` aufgeführten Audio-Abhängigkeiten
NumPy und Faster-Whisper nicht.

### Begriffe und kontrollierte Prüfausschnitte

Ein **ASR-Segment** ist nicht nur der Text und auch nicht nur ein Stück Audio.
Es ist der vom Modell erzeugte Datensatz aus Segmenttext, Startzeit und Endzeit.
Der Bereich zwischen den beiden Zeiten heißt hier **nominelles Zeitintervall**.
Ein **Prüfausschnitt** ist dagegen ein bewusst größeres Audiofenster um dieses
Intervall. So kann ein korrekter Text auch dann akustisch gefunden werden, wenn
seine Zeitgrenzen kollabiert oder verschoben sind.

`review_segment_quality.py` plant für automatisch verdächtige oder explizit
ausgewählte Segmente standardmäßig zwölf Sekunden Kontext auf jeder Seite:

```bash
# Dry-Run
.venv/bin/python review_segment_quality.py AUDIO SEGMENTE

# Verlustfreien FLAC-Ausschnitt und Metadaten lokal erzeugen
.venv/bin/python review_segment_quality.py AUDIO SEGMENTE --confirm

# Zusätzlich mit Faster-Whisper neu transkribieren und vergleichen
.venv/bin/python review_segment_quality.py AUDIO SEGMENTE \
  --confirm --transcribe-local

# Mehrere Aufnahmen mit nur einer Modellladung prüfen
.venv/bin/python review_segment_quality.py \
  --input AUDIO_1 SEGMENTE_1 \
  --input AUDIO_2 SEGMENTE_2 \
  --confirm --transcribe-local

# Einen kollabierten Bereich kontrolliert bis zum nächsten Segment erweitern
.venv/bin/python review_segment_quality.py AUDIO SEGMENTE \
  --extend-to-next-segment --confirm --transcribe-local
```

Die Ausgaben liegen ausschließlich unter `staging/quality-review/`. Die
Neu-Transkription speichert ihre Modell-, Sprach-, Prompt- und
Hotword-Konfiguration reproduzierbar und ordnet ihre relativen Zeitangaben
wieder der Zeitachse der Quellaufnahme zu. Die errechnete Wortabdeckung ist
diagnostisch: Eine geringe Übereinstimmung löst eine menschliche Hörprüfung
aus, aber niemals eine automatische Textänderung.
Zeitangaben der Neu-Transkription, die selbst außerhalb des angeforderten
Prüffensters liegen, bleiben unverändert erhalten, werden aber mit
`timestamp_within_requested_window: false` und einem Warnungszähler markiert.

Der unabhängige Kontrolllauf lässt Prompt und Hotwords standardmäßig weg. Bei
kurzen Ausschnitten kann ein umfangreiches Glossar sonst selbst zum Inhalt der
Erkennung werden. `--use-pipeline-hints` ist deshalb eine ausdrückliche zweite
Messvariante und kein Default. Jede Konfiguration bekommt über ihren Hash eine
eigene Ergebnisdatei; kein vorhandener Vergleich wird überschrieben.

Bei stark kollabierten Endzeiten kann der gesuchte Text erst deutlich nach dem
nominellen Intervall liegen. `--extend-to-next-segment` nutzt dann die nächste
bekannte ASR-Grenze als kontrolliertes Fensterende. Eine Obergrenze von
standardmäßig 90 Sekunden ab dem nominellen Segmentstart verhindert
unbegrenzte Ausschnitte; sie ist mit `--max-extended-seconds` einstellbar.

Der gestufte [Lernplan für lokale KI und den digitalen
Check](LERNPLAN_LOKALE_KI_UND_DIGITALER_CHECK.md) verwendet dieselben
kanonischen Fachbegriffe als dauerhafte Referenz für Erklärungen und
Kundengespräche. Sein Zeitplan dokumentiert die Vorbereitung im September
2026 und ist nicht mehr aktuell.

## Nächster Ausbauschritt: Korrekturbeispiele sammeln

Für eine spätere persönliche Modellanpassung soll die App bestätigte
Korrekturen zusammen mit dem zugehörigen Audiosegment erfassen. Ein Beispiel
besteht mindestens aus:

- Quell-Drive-ID und Audiodatei,
- Start- und Endzeit des Segments,
- unbearbeitetem ASR-Text,
- bestätigtem Korrekturtext,
- betroffenem Begriff und Erstellungszeitpunkt.

Die technische Grundlage aus Segmenttext und Zeitstempeln ist damit vorhanden.
`create_correction_sample.py` plant zunächst ohne Schreibzugriff, welches
Segment zu einem bestätigten Begriff gehört. Nur ein ausdrückliches `--confirm`
schneidet diesen Zeitbereich per FFmpeg ohne Neukodierung aus und schreibt eine
Trainings-Metadatendatei mit Drive-ID, Rohtext, normalisiertem Text,
bestätigtem Korrekturtext und SHA256. Mehrdeutige Treffer müssen durch eine
Segment-ID aufgelöst werden. Reine Hotword-Treffer werden nicht automatisch als
Trainingsbeispiele behandelt.

Die Beispiele bleiben zunächst lokal unter `staging/training-samples/`. Eine
Prüfansicht in der GUI und ein kontrollierter Drive-Upload sind spätere
Ausbauschritte.

Vorgesehene Drive-Struktur:

```text
AudioRec/
├── Recordings/
├── Transcripts/
├── Training Samples/
└── Project Copies/
```

Vor einem Umzug wird der kanonische Aufnahmeordner anhand seiner Drive-ID
bestimmt. Die Aufnahme-App muss nach dem Umzug mit je einem automatischen und
manuellen Testupload geprüft werden. Gleichnamige Ordner werden bis dahin nicht
gelöscht.
