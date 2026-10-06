# Geplanter Ausbau: verlustfreie Audioarchivierung

**Status:** v0.1, lokale Konvertierung v0.2 und verifizierter Drive-Upload v0.3
implementiert; automatische Bereinigung folgt nicht ohne eigene Freigabe

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

## Lokale FLAC-Konvertierung v0.2

`convert_audio_archive.py` übernimmt genau einen `CANDIDATE` aus dem Archivplan.
Ohne Bestätigung zeigt es ausschließlich den lokalen Plan:

```bash
.venv/bin/python convert_audio_archive.py --drive-id DRIVE_ID
```

Erst die ausdrückliche Bestätigung erzeugt unter `staging/audio-archive/` ein
atomar installiertes Paket aus FLAC und `archive.json`:

```bash
.venv/bin/python convert_audio_archive.py \
  --drive-id DRIVE_ID \
  --confirm
```

Vor der Kodierung werden Größe und tatsächlicher Quellhash erneut mit dem
Pipeline-State verglichen. Dafür arbeitet die Konvertierung mit einer temporären
WAV-Momentaufnahme: Erst nachdem deren Größe und Hash feststehen, wird genau
diese unveränderliche Kopie sondiert, kodiert und vollständig dekodiert. Vor der
Paketinstallation wird sie wieder entfernt. WAV und erzeugtes FLAC müssen
jeweils genau eine Audiospur besitzen; dadurch kann keine zusätzliche Spur
unbemerkt entfallen. Nur wenn PCM-SHA256, Dauer, Kanalzahl und Abtastrate
übereinstimmen, wird das Paket installiert. Gleichzeitige Aufrufe für dasselbe
Ziel werden serialisiert;
identische Wiederholungen sind idempotent, abweichende vorhandene Ergebnisse
werden abgelehnt. Das WAV, SQLite und Drive bleiben unverändert.

Auch ein erfolgreich geprüftes lokales Paket setzt `cleanup_ready` weiterhin
auf `false`: Remote-Upload, Remote-Verifikation und Aufbewahrungsrichtlinie
fehlen zu diesem Zeitpunkt noch.

## Verifizierter Drive-Upload v0.3

Das Archivziel muss ein eigener, bereits vorhandener Drive-Unterordner sein.
Es wird ausschließlich über seine unveränderliche Drive-ID adressiert:

```text
AUDIOREC_ARCHIVE_TARGET=gdrive,root_folder_id=DRIVE_ORDNER_ID:
```

Ein normaler Aufruf prüft das lokale FLAC-Paket und zeigt nur den Uploadplan:

```bash
.venv/bin/python upload_audio_archive.py --drive-id DRIVE_ID
```

Erst `--confirm` legt unterhalb des festgelegten Zielordners genau einen
plattformneutral benannten Paketordner an und überträgt dessen FLAC sowie
`archive.json`:

```bash
.venv/bin/python upload_audio_archive.py \
  --drive-id DRIVE_ID \
  --confirm
```

Vor jedem Upload werden das lokale v0.2-Paket und seine Hashes erneut geprüft.
Rclone liest FLAC und Manifest nur aus einer temporären Momentaufnahme, deren
Größe und SHA256 zuvor nochmals mit dem Plan abgeglichen wurden; die
Momentaufnahme wird bei Erfolg und Fehler vollständig entfernt.
Vorhandene Remote-Dateien müssen in Größe und SHA256 exakt passen; abweichende,
doppelte oder unerwartete Dateien brechen den Vorgang ab. Fehlende Dateien eines
unterbrochenen Teiluploads werden mit rclones `--immutable` ergänzt. Nach dem
Upload werden beide Remote-Dateien erneut aufgelistet und anhand von Größe,
SHA256 und Remote-ID verifiziert.

Der erfolgreiche Nachweis wird lokal und atomar als `upload-receipt.json` im
Paket gespeichert. Er bindet Archivschlüssel, ursprüngliche Drive-ID,
Archivziel-ID und die beiden Remote-Datei-IDs zusammen. Eine identische
Wiederholung lädt nichts erneut hoch. WAV, Pipeline-Datenbank und Live-Inbox
bleiben unverändert; `cleanup_ready` bleibt auch nach v0.3 `false`, weil noch
keine Aufbewahrungs- und Löschrichtlinie freigegeben ist.

## Vorgeschlagene Ausbaustufen

- **v0.1:** rein lesender Archivplan ohne Schema- oder Zustandsänderung
- **v0.2:** lokale FLAC-Konvertierung mit technischer Verifikation
- **v0.3:** idempotenter Upload mit Remote-Hashprüfung und lokaler Quittung
- **v0.4:** Aufbewahrungsfrist und ausdrücklich aktivierte Bereinigung

Aufbewahrungsdauer und eine Löschrichtlinie werden erst für v0.4 entschieden.
