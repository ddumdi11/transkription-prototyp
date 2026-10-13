# Übergabe an das Z-System: erste produktive Transkriptlieferung

Stand: 13. Oktober 2026 (erste Lieferung: 25. September 2026)

## Anlass und Ergebnis

Die erste produktive Transkriptlieferung aus `transkription-prototyp` wurde
von Atlas erfolgreich importiert. Die Importquittung meldet:

- `result: imported`
- Übereinstimmung von erwartetem und in Atlas vorhandenem Projektkatalog-Hash
- zwei neu importierte, jeweils per SHA-256 geprüfte Transkript-Assets
- neun ausgeführte Projektzuordnungen
- keine gemeldeten Probleme (`problems: []`)

Die Zuordnungen wurden assetbezogen korrekt umgesetzt: Neben den drei
Sitzungszielen erhielten die beiden Transkripte nur die jeweils für sie
bestätigten segmentbezogenen Projektzuordnungen. Damit ist der technische
Ende-zu-Ende-Weg von der bestätigten Routingentscheidung bis zum Atlas-Import
erstmals erfolgreich belegt.

Die kanonische Liefer-ID lautet:

```text
session-20260922-1501-cb301a63:3dbbb563e1a359a5
```

## Herkunft des Doppelpunkts in `delivery_id`

Der Doppelpunkt stammt aus der Transkriptionspipeline. Bei der ausdrücklichen
Bestätigung eines Sitzungsroutings wird der Manifest-Schlüssel folgendermaßen
gebildet:

```text
<session_id>:<erste 16 Hex-Zeichen des routing_plan_sha256>
```

Der Lieferexporter übernimmt diesen `manifest_key` unverändert als
`delivery_id`. Der Hashanteil sorgt dafür, dass zwei inhaltlich verschiedene
Routingentscheidungen derselben Sitzung nicht dieselbe Lieferidentität
erhalten.

Der Doppelpunkt ist in JSON und in einer logischen Kennung unproblematisch,
aber unter Windows in Datei- und Verzeichnisnamen nicht zulässig. Deshalb
verwendet der Exporter bereits zwei getrennte Darstellungen:

- kanonische Identität in `delivery.json`:
  `session-…:<hash>`
- plattformneutraler Verzeichnisname:
  `session-…__<hash>`

Die Ersetzung betrifft ausschließlich den Namen im Dateisystem. Die
kanonische ID im Manifest und in der Importprovenienz bleibt unverändert.

## Bestätigter v1-Vertrag

Die bestehende `delivery_id` wird für v1 nicht nachträglich geändert.
Sie wurde bereits produktiv ausgeliefert, importiert und quittiert. Eine
Änderung würde zwei Bezeichner für dieselbe Lieferung schaffen oder eine
Migration erfordern, ohne einen funktionalen Vorteil für die fachliche
Identität zu bringen.

Beide Projekte haben folgenden Vertrag bestätigt:

1. `delivery_id` ist eine undurchsichtige fachliche Kennung und darf nicht
   ungeprüft als Datei- oder Verzeichnisname verwendet werden.
2. Für v1 ist der sichere Ablagename deterministisch
   `delivery_id.replace(":", "__")`.
3. Importer speichern und vergleichen weiterhin die kanonische ID aus
   `delivery.json`; die sichere Variante dient nur der Ablage und Suche im
   Dateisystem.
4. Eingangsordner, Archivordner, Sperrdateien, temporäre Dateien und
   Quittungsdateien müssen gleichermaßen die sichere Darstellung verwenden.
5. Die Importlogik muss unter Windows und Linux mit demselben Lieferpaket
   funktionieren.

Die aktuelle v1-Syntax ist eng validiert und enthält selbst keine
Unterstriche. Deshalb ist die Abbildung mit `__` für gültige v1-IDs eindeutig.

Falls später ohnehin ein Schema v2 entsteht, wäre ein zusätzliches explizites
Feld wie `storage_key` die sauberste Weiterentwicklung. Dann müssten nicht
mehrere Implementierungen dieselbe Dateisystemabbildung selbst nachbauen. Eine
alternative, schon in sich dateisystemsichere neue `delivery_id` wäre ebenfalls
möglich, sollte aber nur mit einer neuen Schemaversion eingeführt werden.

## Abgestimmte Importquittung

Die vorliegende Quittung ist funktional ausreichend und belegt einen
erfolgreichen Import. Für künftige Lieferungen wurden drei Vertragspunkte
vereinheitlicht:

### 1. Quittungstyp

Der verbindliche Wert lautet:

```text
z_system_transcript_receipt
```

Damit bilden `z_system_transcript_delivery` und
`z_system_transcript_receipt` ein symmetrisches Paar. Der abweichende Typ der
ersten historischen Quittung macht deren erfolgreichen Import nicht ungültig.

### 2. Hash der vollständigen Lieferung

Künftige Quittungen enthalten mit `delivery_sha256` den SHA-256 der tatsächlich
verarbeiteten `delivery.json`. Für die erste Lieferung lautet dieser Wert:

```text
3c8f9875e22b6bec07f8fea474c64abbaaded2f6a07376be32bd3c625afc625b
```

Damit bindet sich die Quittung eindeutig an genau das verarbeitete
Liefermanifest.

### 3. Ablagepfade

Künftige Quittungen nennen in `stored_path` nur einen relativen POSIX-Pfad
innerhalb des Z-System-Repositories. Das zusätzliche Feld
`storage_root: "z-system-repo"` benennt die Ablagewurzel. Absolute
Windows-Pfade werden nicht mehr übertragen.

Die erste Quittung bleibt bewusst in ihrem historischen Format. Der bestätigte
idempotente Importer schreibt bei einer Wiederholung derselben Lieferung weder
die bestehende Quittung noch Datenbank, Assets oder Zuordnungen neu. Ein
besonderer Weg zur Neuausstellung dieser ersten Quittung ist nicht erforderlich.

## Nächster gemeinsamer Test

Dieselbe unveränderte Lieferung sollte erneut an den Importer übergeben
werden. Erwartetes Verhalten:

- keine zweite Conversation beziehungsweise kein zweites Transkript-Asset
- keine doppelten Projektzuordnungen
- keine Überschreibung abweichender Daten
- eindeutige Quittung als bereits vorhanden beziehungsweise idempotent
  verarbeitet
- weiterhin keine Probleme

Damit wird neben Integrität und Zuordnung auch die Wiederholbarkeit nach
Abbruch, Neustart oder versehentlicher Mehrfachzustellung praktisch belegt. Der
Atlas-Importer ist bereits auf das erwartete Ergebnis `already_imported`
ausgelegt. Der Wiederholungstest und ein pipeline-seitiger, rein lesender
Quittungsvalidator stehen noch aus.

## Ergebnis der Abstimmung

Die zuvor offenen Vertragspunkte sind geklärt:

1. kanonische v1-`delivery_id` bleibt unverändert;
2. sichere Dateisystemabbildung verwendet `:` zu `__`;
3. `receipt_type` ist `z_system_transcript_receipt`;
4. `delivery_sha256` bindet die Quittung an `delivery.json`;
5. `stored_path` ist relativ zu `storage_root: "z-system-repo"`;
6. eine unveränderte Wiederholung liefert `already_imported` und nimmt keine
   erneuten Schreiboperationen vor.
