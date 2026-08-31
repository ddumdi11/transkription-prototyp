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

## Nächster Ausbauschritt: Korrekturbeispiele sammeln

Für eine spätere persönliche Modellanpassung soll die App bestätigte
Korrekturen zusammen mit dem zugehörigen Audiosegment erfassen. Ein Beispiel
besteht mindestens aus:

- Quell-Drive-ID und Audiodatei,
- Start- und Endzeit des Segments,
- unbearbeitetem ASR-Text,
- bestätigtem Korrekturtext,
- betroffenem Begriff und Erstellungszeitpunkt.

Die Transkriptionsschnittstelle muss dafür zunächst Segmenttexte und
Zeitstempel erhalten, statt sie sofort zu einem einzigen Text zu verbinden.
Erst eine bestätigte Ersetzung löst das verlustfreie Ausschneiden des
Audiosegments und das Schreiben einer Metadatendatei aus. Reine Hotword-Treffer
werden nicht automatisch als Trainingsbeispiele behandelt.

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
