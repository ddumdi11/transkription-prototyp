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
Als nächster Schritt soll erst eine bestätigte Ersetzung das verlustfreie
Ausschneiden des Audiosegments und das Schreiben einer Trainings-Metadatendatei
auslösen. Reine Hotword-Treffer werden nicht automatisch als Trainingsbeispiele
behandelt.

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
