# Geplanter Ausbau: verlustfreie Audioarchivierung

**Status:** v0.1 als rein lesender Dry-Run implementiert; Konvertierung folgt

Die WAV-Dateien der Live-Inbox belegen auf Google Drive zunehmend viel Platz.
Nach vollständig abgeschlossener Verarbeitung sollen geeignete Originale daher
verlustfrei als FLAC archiviert werden. MP3 bleibt eine spätere optionale
Exportform für besonders kleine Hörkopien, ist aber wegen der verlustbehafteten
Kodierung nicht das kanonische Archivformat.

## Sicherheitsgrenzen

- Die Live-Inbox und das Archiv sind getrennte, per Drive-ID konfigurierte
  Ordner. Archivdateien dürfen nicht erneut als neue Inbox-Aufnahmen erscheinen.
- Ein Kandidat muss mindestens erfolgreich transkribiert (`DONE`) und sein
  kanonisches Transkript erfolgreich veröffentlicht (`PUBLISHED`) sein.
- Offene Qualitätsprüfungen dürfen nicht durch eine vorzeitige Entfernung des
  WAV-Originals erschwert werden. Dafür benötigt die Pipeline vor automatischen
  Löschungen einen persistenten QA- beziehungsweise Freigabestatus.
- Konvertierung, Upload und Prüfung sind wiederholbar und werden anhand der
  ursprünglichen Drive-ID sowie der Hashes von WAV und FLAC protokolliert.
- Weder lokale noch entfernte WAV-Dateien werden in der ersten Ausbaustufe
  gelöscht. Löschen wird erst nach einer festgelegten Aufbewahrungsfrist und
  einer ausdrücklich aktivierten Richtlinie möglich.

## Geplanter Ablauf

1. Archivkandidaten ausschließlich lesend aus dem Pipeline-Zustand ermitteln.
2. Einen Dry-Run mit Quell-ID, Dateigröße, Abschlussstatus und erwartetem Ziel
   ausgeben.
3. WAV lokal mit FFmpeg nach FLAC konvertieren; das Original bleibt erhalten.
4. FLAC vollständig dekodieren und Dauer, Kanäle sowie Abtastrate mit dem
   Original vergleichen.
5. SHA256, Größenersparnis und Herkunft persistent speichern.
6. FLAC in einen eigenen Drive-Archivordner hochladen und den entfernten Inhalt
   verifizieren.
7. Erst in einer späteren, getrennt freizugebenden Stufe abgelaufene
   WAV-Originale entfernen.

## Archiv-Dry-Run v0.1

Der Archivplaner öffnet den bestehenden Pipeline-State im SQLite-Read-only-
Modus. Er erzeugt weder Tabellen noch Journalzustände und ändert keine Audio-,
Transkript- oder Drive-Datei:

```bash
.venv/bin/python plan_audio_archive.py
```

Ein künftiges rclone-Ziel kann schon für die erwarteten Pfade angegeben werden,
ohne dass der Dry-Run darauf zugreift:

```bash
.venv/bin/python plan_audio_archive.py \
  --archive-target 'gdrive,root_folder_id=DRIVE_ORDNER_ID:' \
  --verbose
```

Alternativ wird das Ziel aus `AUDIOREC_ARCHIVE_TARGET` gelesen. `--json` gibt
den vollständigen maschinenlesbaren Plan aus; `--drive-id ID` begrenzt ihn auf
eine oder mehrere konkrete Aufnahmen. Der Platzhalter steht für die ID eines
eigenen Archiv-Unterordners innerhalb von „Z-System-Backups auf Drive“. Die
ID-basierte rclone-Adresse verhindert, dass ein gleichnamiger Drive-Ordner zum
falschen Ziel wird.

`CANDIDATE` bedeutet in v0.1 nur: WAV liegt lokal mit der erwarteten Größe und
der beim geprüften Staging festgehaltenen Drive-Identität vor, der Job ist
`DONE`, und das kanonische Transkript wurde veröffentlicht. Der Planer führt die
Segmentprüfung erneut aus und gleicht Auffälligkeiten mit den persistenten
QA-Entscheidungen ab. Offene oder bestätigte Auffälligkeiten verhindern die
verlustfreie Archivkopie nicht, blockieren aber eine spätere Bereinigung.
`cleanup_ready` bleibt in v0.1 ausnahmslos `false`, weil weder ein verifiziertes
FLAC noch eine Aufbewahrungsrichtlinie existiert.

## Vorgeschlagene Ausbaustufen

- **v0.1:** rein lesender Archivplan ohne Schema- oder Zustandsänderung
- **v0.2:** lokale FLAC-Konvertierung mit technischer Verifikation
- **v0.3:** idempotenter Upload in den Drive-Archivordner
- **v0.4:** Aufbewahrungsfrist und ausdrücklich aktivierte Bereinigung

Vor v0.2 muss der per Drive-ID eindeutig adressierte Zielordner festgelegt
werden. Aufbewahrungsdauer und eine Löschrichtlinie werden erst für v0.4
entschieden.
