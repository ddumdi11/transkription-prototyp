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
bleiben zunächst lokal unter `staging/`; Drive wird nicht verändert.

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

## Projektverteilung planen (v0.3 Dry-Run)

`route_transcripts.py` plant die projektbezogene Verteilung bereits erfolgreich
veröffentlichter Transkripte. Das kanonische Transkript in `AudioRec Transcripts`
bleibt unverändert. Der Dry-Run kopiert keine Datei und ändert nichts auf Drive.

Die lokale, von Git ausgeschlossene Konfiguration wird einmalig aus dem Beispiel
angelegt:

```bash
cp routing.example.json .inbox-watcher/routing.json
```

`default_projects` erhalten jedes Transkript. `active_projects` beschreiben den
aktuellen Arbeitskontext und erhalten während dieser Phase ebenfalls jedes
Transkript. `project_rules` ergänzen Projekte anhand transparenter Suchbegriffe;
`topic_rules` vergeben davon unabhängige Themen-Tags.

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
