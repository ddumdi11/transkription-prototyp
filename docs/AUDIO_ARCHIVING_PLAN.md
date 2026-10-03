# Geplanter Ausbau: verlustfreie Audioarchivierung

**Status:** Roadmap, noch nicht implementiert

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

## Vorgeschlagene Ausbaustufen

- **v0.1:** rein lesender Archivplan und SQLite-Schema
- **v0.2:** lokale FLAC-Konvertierung mit technischer Verifikation
- **v0.3:** idempotenter Upload in den Drive-Archivordner
- **v0.4:** Aufbewahrungsfrist und ausdrücklich aktivierte Bereinigung

Vor der Implementierung müssen Zielordner, Aufbewahrungsdauer und das Verhalten
bei offenen QA-Kandidaten gemeinsam festgelegt werden.
