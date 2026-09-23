# Inbox-Wächter v0.1 (Dry-Run)

Der normale Wächterlauf liest die Dateiliste aus `gdrive:AudioRec Recordings`,
führt lokalen Zustand und klassifiziert Uploads. Dabei lädt, verschiebt, löscht
oder transkribiert er nichts. Nur das ausdrücklich angegebene `--stage-id` lädt
eine ausgewählte Datei nach lokaler Größen- und Hashprüfung ins lokale Staging.
Google Drive wird auch dabei nicht verändert.

```bash
cd ~/src/Projects_Cloned-Github/transkription-prototyp
.venv/bin/python inbox_watcher.py
```

Standardmäßig muss eine Datei bei zwei Prüfungen mit mindestens 120 Sekunden
Abstand in Größe und Hash unverändert sein. Erst dann wird sie `READY`.

Statuswerte:

- `OBSERVED`: erstmalig gesehen, verändert oder Abstand noch zu kurz
- `READY`: bei zwei ausreichend weit auseinanderliegenden Prüfungen unverändert
- `DUPLICATE`: anderer Drive-Upload mit bereits bekanntem, bereitem Inhaltshash
- `IGNORED`: kein unterstütztes Audio, leer, ohne Drive-ID oder ohne Hash

Lokaler Zustand und Logs liegen unter `.inbox-watcher/`. Für einen schnelleren
manuellen Funktionstest kann der Mindestabstand vorübergehend geändert werden:

```bash
.venv/bin/python inbox_watcher.py --stable-seconds 10 --json
```

`--listing-file DATEI` liest eine gespeicherte `rclone lsjson`-Ausgabe und ist
für Tests gedacht. Auch ohne `--listing-file` bleibt der Lauf ein Drive-Dry-Run.

Der normale Lauf zeigt nur eine kompakte Zusammenfassung. `--verbose` schreibt
eine Logzeile pro Datei; `--json` gibt bei Bedarf sämtliche Dateidetails aus.

Neue Uploads der letzten 30 Minuten kompakt anzeigen:

```bash
.venv/bin/python inbox_watcher.py --recent-minutes 30
```

Eine einzelne `READY`-Datei anhand ihrer exakten Drive-ID lokal bereitstellen:

```bash
.venv/bin/python inbox_watcher.py --stage-id DRIVE_ID
```

Die Datei landet mit einem kollisionssicheren Namen unter `staging/inbox/`.
Größe und Inhaltshash werden nach dem Download geprüft. Ein erneuter identischer
Aufruf ist idempotent. Drive wird dabei weder gelöscht noch verschoben.

## Automatische lokale Transkription

`inbox_pipeline.py` verarbeitet nur `READY`-Originale ab einer explizit
gesetzten Aktivierungsgrenze. Erfolgreiche Jobs werden persistent gespeichert
und nicht wiederholt. Dubletten werden nie verarbeitet. Audio und Transkripte
bleiben zunächst lokal unter `staging/`; Drive wird nicht verändert. Neben dem
Markdown erzeugt die lokale Engine `<Name>.segments.json` mit Zeitgrenzen und
Rohtext jedes faster-whisper-Segments sowie der eindeutigen Drive-ID als
`source_id`. Ein Job wird nur `DONE`, wenn beide Dateien geschrieben wurden.

Die Vorlagen unter `systemd/` prüfen im Abstand von drei Minuten.

Timerstatus anzeigen:

```bash
systemctl --user status transkription-inbox.timer --no-pager
```

Automatik anhalten beziehungsweise wieder starten:

```bash
systemctl --user stop transkription-inbox.timer
systemctl --user start transkription-inbox.timer
```

Der lokale Betriebszustand liegt unter `.inbox-watcher/state.sqlite3`. Datierte
Sicherungen können unter `.inbox-watcher/backups/` abgelegt werden. Audio- und
Transkriptdateien unter `staging/` sowie sämtliche Laufzeitdaten sind bewusst
von Git ausgeschlossen.

Wenn Drive mehrere Ordner mit demselben Namen enthält, sollte die Quelle über
die eindeutige Ordner-ID in `.inbox-watcher/pipeline.env` festgelegt werden:

```text
AUDIOREC_SOURCE=gdrive,root_folder_id=DRIVE_ORDNER_ID:
```

Die systemd-Vorlage lädt diese lokale, von Git ausgeschlossene Konfiguration.

### Abgelaufene Drive-Anmeldung

Wenn Google das rclone-Refresh-Token mit `invalid_grant` ablehnt, schreibt die
Pipeline die vollständige rclone-Fehlermeldung ins Journal und zeigt eine
kritische Desktop-Benachrichtigung mit dem Reparaturbefehl an:

```bash
rclone config reconnect gdrive:
```

Eine Markerdatei unter `.inbox-watcher/rclone-auth-required` hält den Fehler
auch nach dem Benachrichtigungsfenster fest. Weitere Hinweise werden für sechs
Stunden unterdrückt. Nach dem nächsten erfolgreichen Drive-Zugriff entfernt die
Pipeline den Marker automatisch.

Für die lokale Erkennung können Kontext-Prompt und Hotwords ebenfalls dort
gepflegt werden. Hotwords beeinflussen die Erkennung, führen aber noch keine
nachträgliche Textersetzung aus:

```text
AUDIOREC_PROMPT=Fachbegriffe: Diktiergerät, Claude, Claude Code, KI
AUDIOREC_HOTWORDS=Traktat
```

## Transkripte veröffentlichen (v0.2)

`publish_transcripts.py` zeigt standardmäßig nur den Plan aller erfolgreichen,
noch nicht veröffentlichten Transkripte. Es lädt ohne `--publish-id` nichts hoch.

```bash
.venv/bin/python publish_transcripts.py
```

Das Ziel wird ebenfalls per eindeutiger Drive-Ordner-ID in
`.inbox-watcher/pipeline.env` konfiguriert:

```text
AUDIOREC_TRANSCRIPTS_TARGET=gdrive,root_folder_id=DRIVE_ORDNER_ID:
```

Ein einzelnes Transkript wird anhand der ursprünglichen Audio-Drive-ID explizit
veröffentlicht:

```bash
.venv/bin/python publish_transcripts.py --publish-id AUDIO_DRIVE_ID
```

Nach geprüftem Einzeltest kann der gesamte geplante Rückstand ausdrücklich
verifiziert beziehungsweise veröffentlicht werden:

```bash
.venv/bin/python publish_transcripts.py --publish-all
```

Bereits vorhandene, identische Remote-Dateien werden übernommen und nicht erneut
hochgeladen. `--verbose` zeigt bei Bedarf alle geplanten Jobs einzeln an.

Lokale und entfernte Größe sowie SHA256 werden geprüft, bevor der persistente
Status `PUBLISHED` gespeichert wird. Wiederholte Aufrufe erzeugen keine Dublette.

Nach abgeschlossenem Einzel- und Rückstandstest kann die automatische
Veröffentlichung in `.inbox-watcher/pipeline.env` aktiviert werden:

```text
AUDIOREC_AUTO_PUBLISH=1
```

Ohne diesen expliziten Wert bleibt die automatische Veröffentlichung aus. Bei
einem Uploadfehler bleibt der Transkriptionsjob `DONE` und wird beim nächsten
Timerlauf erneut zur Veröffentlichung angeboten.

Die Pipeline holt solche liegengebliebenen `DONE`-Jobs zu Beginn eines Laufs
nach. Anschließend veröffentlicht sie jedes neu erzeugte Transkript unmittelbar
nach dessen `DONE`-Status. Ein langes späteres Diktat hält damit früher fertige
Transkripte nicht mehr von Drive zurück.

Die Veröffentlichung lädt vorerst weiterhin nur das kanonische
Markdown-Transkript hoch. Die Segmentdatei bleibt lokal unter
`staging/transcripts/`, bis die geplante Bestätigung von Korrekturbeispielen und
die Übergabe an das Z-System ein eigenes, geprüftes Ziel erhalten.

## Bestätigte Korrekturbeispiele vorbereiten

`create_correction_sample.py` verbindet eine lokale Aufnahme ausdrücklich mit
ihrer validierten Segmentdatei. Ohne `--confirm` zeigt es nur den Plan. Ein
eindeutig vorkommender bestätigter Begriff wählt sein Segment automatisch;
mehrere Treffer erfordern `--segment-id`. Erst mit `--confirm` wird der
Zeitbereich per FFmpeg ohne Neukodierung nach `staging/training-samples/`
ausgeschnitten und zusammen mit Herkunft, Drive-ID, Rohtext, bestätigt
korrigiertem Text und SHA256 dokumentiert.

Ein bloßer Hotword-Treffer löst diesen Vorgang niemals aus. Das Werkzeug ändert
keinen Pipeline-Status, keine Aufnahme und kein kanonisches Transkript. Ein
Drive-Upload der Trainingsbeispiele ist noch nicht aktiviert.

## Projektverteilung planen (v0.3 Dry-Run)

### Atlas-Projektkatalog prüfen

Atlas exportiert seinen versionierten Vorhabenkatalog als UTF-8-JSON. Vor
jeder späteren Routing- oder Exportverwendung wird eine lokale Kopie
ausschließlich lesend validiert:

```bash
.venv/bin/python project_catalog.py /pfad/zu/atlas-project-catalog.v1.json
```

Der Validator prüft neben der Struktur unter anderem stabile slugförmige
Projekt-IDs, Status und Kategorien, eindeutige Begriffe, gültige Beziehungen,
`exact_terms` als Teilmenge von `terms` sowie den kanonischen `catalog_hash`.
Das maschinenlesbare Vertragsschema liegt unter
`schemas/atlas-project-catalog.schema.v1.json`. Der Befehl verändert weder den
Katalog noch Pipeline-Status oder Drive.

`route_transcripts.py` plant die projektbezogene Verteilung bereits erfolgreich
veröffentlichter Transkripte. Das kanonische Transkript in `AudioRec Transcripts`
bleibt unverändert. Der Dry-Run kopiert keine Datei und ändert nichts auf Drive.

Nach jeder erfolgreichen automatischen Veröffentlichung erzeugt die Pipeline
denselben Routingplan für genau dieses Transkript und schreibt Projekte,
Begründungen und Themen ins Journal. Dieser Schritt ist weiterhin rein beratend:
Er kopiert keine Dateien und verändert Drive nicht. Ein Routingfehler wird als
`Routing FAILED` protokolliert, ändert aber weder `PUBLISHED` noch die weitere
Transkriptionswarteschlange.

Die lokale, von Git ausgeschlossene Konfiguration wird einmalig aus dem Beispiel
angelegt:

```bash
cp routing.example.json .inbox-watcher/routing.json
```

`default_projects` erhalten jedes Transkript. `active_projects` beschreiben den
aktuellen Arbeitskontext und erhalten während dieser Phase ebenfalls jedes
Transkript. `project_rules` ergänzen Projekte anhand transparenter Suchbegriffe;
`topic_rules` vergeben davon unabhängige Themen-Tags. Begriffe in `exact_terms`
müssen an einer Wortgrenze enden. So trifft etwa `Drive` weiterhin auf
`Drive-Ordner`, aber nicht auf `Driven` in „Test Driven Development“.

Den Plan für einen Aufnahmebereich anzeigen:

```bash
.venv/bin/python route_transcripts.py --from-number 565 --to-number 570
```

Maschinenlesbare Ausgabe:

```bash
.venv/bin/python route_transcripts.py --from-number 565 --to-number 570 --json
```

Jedes Ziel wird mit `default`, `active_context` oder den passenden
`content:`-Begriffen begründet. In v0.3 erzeugt das Werkzeug ausschließlich den
Plan; die idempotente Kopierfunktion folgt erst nach geprüftem Dry-Run.

## Aufnahme-Sitzungen planen (Dry-Run)

`plan_transcript_sessions.py` fasst bereits veröffentlichte Aufnahmen eines
Tages zu zusammengehörigen Denk-, Arbeits- oder Spaziergangssitzungen zusammen.
Es verändert weder Transkripte noch Pipeline-Status oder Drive. Voraussetzung
sind die von der Pipeline erzeugten lokalen Segmentdateien.

```bash
.venv/bin/python plan_transcript_sessions.py --date 2026-09-19
```

Die Drive-Änderungszeit gilt als Aufnahmeende. Das Ende des letzten validierten
Whisper-Segments dient als Näherung für die Aufnahmedauer und damit für den
Aufnahmebeginn. Entscheidend ist anschließend die geschätzte Ruhezeit zwischen
zwei Aufnahmen, nicht deren Upload- oder Transkriptionszeit. Standardmäßig
beginnt erst nach mehr als 90 Minuten eine neue Sitzung:

```bash
.venv/bin/python plan_transcript_sessions.py \
  --date 2026-09-19 --max-gap-minutes 60 --json
```

Grenzfälle lassen sich anhand der unverwechselbaren Audio-Drive-ID korrigieren:

```bash
# Vor dieser Aufnahme immer trennen
.venv/bin/python plan_transcript_sessions.py --date 2026-09-19 \
  --break-before DRIVE_ID

# Diese Aufnahme trotz großer Pause mit der vorherigen verbinden
.venv/bin/python plan_transcript_sessions.py --date 2026-09-19 \
  --join-with-previous DRIVE_ID
```

Eine Drive-ID darf nicht gleichzeitig beide Regeln erhalten. Unbekannte IDs und
fehlende oder ungültige Segmentdaten führen zu einem kontrollierten Fehler,
statt stillschweigend einen unzuverlässigen Plan zu erzeugen. Die Sitzungs-ID
basiert auf der ersten Aufnahme und bleibt stabil, wenn später weitere
Aufnahmen an dieselbe Sitzung angehängt werden.

## Sitzungen segmentgenau Projekten zuordnen (Dry-Run)

`plan_session_routing.py` verbindet die Sitzungsplanung mit den vorhandenen
Projekt- und Themenregeln. Der Befehl öffnet die Pipeline-Datenbank nur lesend,
schreibt kein Manifest und verändert weder Transkripte noch Status oder Drive.

```bash
.venv/bin/python plan_session_routing.py --date 2026-09-19
```

Die kompakte Ausgabe zeigt je Sitzung die sitzungsweiten Projektziele, die Zahl
inhaltlich belegter Segmente, Themen und noch nicht projektspezifisch erkannter
Segmente. Der vollständige Plan ist als JSON verfügbar:

```bash
.venv/bin/python plan_session_routing.py --date 2026-09-19 --json
```

Projektzuordnungen unterscheiden drei Eigenschaften:

- `scopes: ["default"]`: Das Projekt erhält grundsätzlich die ganze Sitzung.
- `scopes: ["active_context"]`: Das Projekt gehört zum aktuellen Arbeitskontext
  und erhält ebenfalls die ganze Sitzung.
- `scopes: ["content"]`: Mindestens ein einzelnes Segment erfüllt eine
  transparente Inhaltsregel. `segments` enthält dafür Drive-ID, Aufnahme,
  Segment-ID, relative und geschätzte absolute Zeit, Text und Trefferbegriffe.

Ein Projekt kann mehrere dieser Eigenschaften gleichzeitig besitzen. Mehrere
Regeln desselben Projekts werden je Segment zusammengeführt. Themen enthalten
dieselben segmentgenauen Belege, wirken aber nicht als Projektziel.

`unassigned_segments` bedeutet ausschließlich, dass für diese Segmente noch
keine projektspezifische Inhaltsregel getroffen hat. Sie gehen nicht verloren:
`default`- und `active_context`-Projekte gelten weiterhin für die vollständige
Sitzung. Diese Trennung zeigt zugleich, welche Inhalte später vom Projektkatalog
oder einer optionalen intelligenten Klassifikation profitieren würden.

Zeitgrenze und manuelle Sitzungsregeln entsprechen dem Sitzungsplaner:

```bash
.venv/bin/python plan_session_routing.py --date 2026-09-19 \
  --max-gap-minutes 60 --break-before DRIVE_ID
```

### Geprüften Routingplan lokal bestätigen

Der Sitzungs-Routingplan bleibt standardmäßig ein Dry-Run. Nach der inhaltlichen
Prüfung können eine oder mehrere angezeigte Sitzungs-IDs ausdrücklich bestätigt
werden:

```bash
.venv/bin/python plan_session_routing.py --date 2026-09-19 \
  --confirm-session session-20260919-1124-5bd7d0b0
```

Erst `--confirm-session` schreibt ein lokales JSON-Manifest nach
`staging/routing-manifests/`. Die Option ist wiederholbar, wenn mehrere
Sitzungen desselben Tages gemeinsam geprüft wurden. Vor dem ersten Schreiben
müssen sämtliche genannten IDs im aktuellen Tagesplan vorkommen.

Das Manifest enthält den vollständigen Routingplan, den UTC-Zeitpunkt der
ausdrücklichen CLI-Bestätigung und einen SHA256 über den kanonisch serialisierten
Plan. Eine identische Wiederholung ist idempotent. Existiert unter derselben
Sitzungs-ID bereits ein beschädigtes oder inhaltlich abweichendes Manifest,
wird es nicht überschrieben, sondern als Konflikt gemeldet. Die Installation
erfolgt über eine temporäre Datei; Fehler hinterlassen kein Teilmanifest.

Ein alternatives lokales Ziel lässt sich explizit angeben:

```bash
.venv/bin/python plan_session_routing.py --date 2026-09-19 \
  --confirm-session SESSION_ID --manifest-dir /lokales/ziel
```

Auch die Bestätigung kopiert noch keine Projektdateien und nimmt keinerlei
Drive- oder Pipeline-Statusänderung vor. Das bestätigte Manifest ist die
prüfbare Eingabe für den späteren Export in Projekt-Eingänge.
