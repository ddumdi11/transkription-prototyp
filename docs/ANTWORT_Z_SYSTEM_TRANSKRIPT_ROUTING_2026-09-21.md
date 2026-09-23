# Antwort an das Z-System zum Transkript-Routing

**Stand:** 21. September 2026  
**Bezug:** Rückmeldung zum Schnittstellenentwurf vom 21. September 2026

## Gesamteinschätzung

Die Rückmeldung ist mit der bestehenden Pipeline-Architektur weitgehend
kompatibel. Die vorgeschlagene Trennung bleibt erhalten:

- Atlas besitzt Vorhaben-Identitäten, Namen, Status, Aliasse und fachliche
  Routingbegriffe.
- Die Transkriptionspipeline besitzt Audio-, Transkript-, Segment- und
  Sitzungsidentitäten.
- Google Drive dient als append-only Übergabekanal.
- Anzeigenamen und Ordnerpfade sind keine Identitäten.
- Kanonische Transkripte werden nicht verändert.

Die folgenden Anpassungen des ersten Entwurfs werden fachlich empfohlen.

## Übernommene Anpassungen

### 1. Vorhaben-Slug als `project_id`

`project_id` soll dem stabilen Atlas-`project_key` des Vorhabens entsprechen,
ohne zusätzliches Präfix. Container und Sentinels werden nicht exportiert.

Beispiele:

```text
somas
z01
win-testautomatisierung
bildwissenschaft
```

Der Statusvorrat wird um `candidate` ergänzt:

```text
active | paused | archived | candidate
```

### 2. Keine Vorhaben-Hierarchie in Version 1

`relations` bleibt im Schema zulässig, ist in der ersten Fassung aber leer.
Die Pipeline liest und validiert das Feld, leitet daraus zunächst jedoch keine
Routingentscheidung ab.

### 3. Vollständige Transkripte bei jedem Import

Auch bei `scope: "segments"` importiert Atlas stets das vollständige,
unveränderte Transkript. Segment-IDs und Zeitbezüge dokumentieren nur die
Begründung der Projektzuordnung. Die Pipeline erzeugt keine bewertenden
Textauszüge als Ersatz für die Quelle.

### 4. Transkripte als Atlas-Conversations

Die Abbildung auf bestehende Atlas-Entitäten ist technisch schlüssig:

```text
conversations.source      = "audiorec"
conversations.external_id = canonical_drive_id
conversations.content_hash = SHA256 des kanonischen Markdown-Transkripts
```

Die Projektzuordnung erfolgt N:M über `conv_project`. `scope`, `reasons` und
`segments` bleiben als Herkunfts- und Begründungsmetadaten erhalten.

Vor der Umsetzung sollte Atlas sicherstellen, dass die Eindeutigkeitsregel für
`external_id` mindestens die Quelle berücksichtigt, also sinngemäß
`(source, external_id)` eindeutig ist.

### 5. Feldaufteilung und Eigentümerschaft

| Information | Eigentümer |
|---|---|
| `project_id`, `name`, `status`, `category` | Atlas / Z01-Register |
| Container-Namen als `aliases` | Atlas |
| `routing.terms`, `routing.exact_terms` | Nachtwächter-Ontologie über Atlas-Export |
| ASR-Hotwords und sichere Textersetzungen | Transkriptionspipeline |
| `default_projects`, `active_projects` | lokale Routingpolitik der Pipeline |

`kind` aus dem ersten Entwurf wird durch die Z01-Registerkategorie
`category` ersetzt. Die Pipeline behandelt die Kategorie zunächst als
beschreibende Information.

Für die Routingsemantik gilt nach Prüfung des Atlas-Exports:

- `routing.terms` enthält das vollständige kuratierte Vokabular und liefert
  Kontext- beziehungsweise Stützsignale.
- `routing.exact_terms` ist eine Teilmenge sicherer Einzelbegriffe. Jeder
  Treffer darf allein eine Zuordnung begründen und muss zugleich strikt an
  echten Wortgrenzen erfolgen.
- Mehrteilige Begriffe und Aliasse stehen nur in `terms`; die Pipeline bewertet
  sie als starke Stützsignale, aber nicht automatisch als Einzelbeweis.

### 6. Kataloghash

Zusätzlich zu `catalog_revision` enthält der Export `catalog_hash`. Damit beide
Seiten denselben Hash bilden, wird für Version 1 folgende Kanonisierung
vorgeschlagen:

1. Projekte aufsteigend nach `project_id` sortieren;
2. JSON-Objektschlüssel lexikografisch sortieren;
3. UTF-8, keine ASCII-Ersatzsequenzen;
4. keine unbedeutenden Leerzeichen;
5. keine Werte `NaN` oder `Infinity`;
6. SHA256 über das kanonisierte `projects`-Array.

In Python entspricht der Kern:

```python
json.dumps(
    sorted(projects, key=lambda item: item["project_id"]),
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
    allow_nan=False,
).encode("utf-8")
```

### 7. Outbox und Aufbewahrung

Der Outbox-Ordner liegt auf der obersten Drive-Ebene neben den bestehenden
AudioRec-Ordnern und wird auf beiden Seiten über seine Drive-ID beziehungsweise
den lokal synchronisierten Ordner konfiguriert.

Quittierte Pakete werden in Version 1 nicht automatisch gelöscht, verschoben
oder überschrieben.

## Präzisierung für Sitzungen mit mehreren Aufnahmen

Eine Sitzung kann mehrere kanonische Transkripte enthalten. Deshalb gilt beim
Atlas-Import:

1. Jedes Asset mit einer eigenen `canonical_drive_id` erzeugt höchstens eine
   `conversations`-Zeile.
2. Eine Zuordnung mit `scope: "whole_session"` wird auf alle Transkript-Assets
   der Sitzung angewandt.
3. Eine Zuordnung mit `scope: "segments"` wird nur auf diejenigen
   Transkript-Assets angewandt, deren `source_audio_drive_id` in den
   Segmentbelegen vorkommt.
4. `delivery_id` und `session_id` sollen als Importprovenienz erhalten bleiben,
   damit die ursprüngliche Sitzung später rekonstruierbar ist.
5. Wiederholte Assets mit gleicher `(source, external_id)` und gleichem Hash
   sind idempotent. Ein abweichender Hash ist ein Konflikt.

Ob Atlas `delivery_id` und `session_id` in einem vorhandenen Metadatenfeld oder
in einer kleinen Import-Provenienztabelle speichert, entscheidet das
Atlas-Projekt.

## Einordnung der bisherigen Standardprojekte

Die Z03-Ablage und die fachliche Vorhabenzuordnung sind zwei verschiedene
Ebenen:

- Jedes vollständige Transkript wird als Quelle in Z03 abgelegt.
- Zusätzlich kann dieselbe Conversation einem oder mehreren echten Vorhaben
  zugeordnet werden.

Die bisherige Bezeichnung `Z04 - MyOwnCents` darf daher nicht als technische
Projekt-ID exportiert werden. Die spätere Entscheidung legt stattdessen
`projektverwaltung-steuerung` und `selbstregulation` als vorläufige lokale
`default_projects` fest. Für `selbstregulation` gelten sowohl „Selbstregulation
statt Selbstverbesserung“ als auch die frühere Formulierung „Selbstregulation
statt Selbstoptimierung“ als Aliasse.

## Aktualisierte Implementierungsreihenfolge

1. Z02 bestätigt die vier Datenmodellentscheidungen.
2. Atlas exportiert einen kleinen realen Projektkatalog mit stabilen Slugs,
   Kandidatenstatus, Container-Aliassen und Ontologiebegriffen.
3. Beide Seiten einigen sich auf ein JSON Schema und die exakte
   `catalog_hash`-Kanonisierung.
4. Die Pipeline implementiert lesende Katalogvalidierung und löst bisherige
   Projektnamen auf Atlas-Slugs auf.
5. Die Pipeline ergänzt manuelle Projektzugaben und -entfernungen bei der
   Sitzungsbestätigung.
6. Pipeline-Exporter und Atlas-Importer werden gegen dieselben festen
   Beispieldateien entwickelt; beide beginnen als Dry-Run.
7. Der Outbox-Ordner wird angelegt und über seine Drive-ID konfiguriert.
8. Ein echter Spaziergang wird End-to-End exportiert, importiert und quittiert.
9. Erst nach erfolgreicher Wiederholung wird die Übergabe automatisiert.

## Empfohlene Entscheidungen für Z02

1. **`project_id` = Vorhaben-Slug ohne Präfix:** ja.
2. **Transkripte als `conversations` mit `source = "audiorec"`:** ja, sofern
   Importprovenienz und quellenbezogene Eindeutigkeit gesichert werden.
3. **Bei `scope: "segments"` trotzdem vollständiger Import:** ja.
4. **Pipeline-Standardprojekte:** vorläufig `projektverwaltung-steuerung` und
   `selbstregulation`; `Z04` bleibt eine Rolle und ein Containeralias.
5. **Outbox:** zentraler Drive-Ordner, niemals automatisch löschen; Drive-ID
   nach Anlage in beiden Projekten konfigurieren.
