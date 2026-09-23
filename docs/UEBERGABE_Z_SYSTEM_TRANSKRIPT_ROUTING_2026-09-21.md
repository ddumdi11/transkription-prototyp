# Übergabe an das Z-System: Projektkatalog und Transkript-Routing

**Stand:** 21. September 2026  
**Quellprojekt:** `transkription-prototyp`  
**Zielprojekt:** Z-System / Atlas-App

## Zweck

Diese Notiz beschreibt den aktuellen Stand der Transkriptionspipeline und eine
technische Schnittstelle, über die Atlas Projektinformationen bereitstellen und
bestätigte Transkripte aus Google Drive abholen kann.

Die kanonischen Transkripte in `AudioRec Transcripts` bleiben unverändert. Eine
Projektzuordnung erzeugt niemals eine neue kanonische Fassung, sondern nur ein
nachvollziehbares Übergabepaket beziehungsweise eine Projektkopie.

## Aktueller Stand der Pipeline

Die produktiv laufende Pipeline kann bereits:

1. neue Audiodateien anhand Drive-ID und Inhaltshash erkennen;
2. unvollständige Uploads durch zwei zeitversetzte Beobachtungen abfangen;
3. echte Dubletten überspringen und gleichnamige Dateien mit anderem Inhalt
   getrennt behandeln;
4. Audios lokal mit faster-whisper transkribieren;
5. Segmentdateien mit Zeitgrenzen, ASR-Rohtext und normalisiertem Text erzeugen;
6. Transkripte geprüft und idempotent nach `AudioRec Transcripts` hochladen;
7. Aufnahmen anhand Aufnahmezeit und Ruheabstand zu Sitzungen gruppieren;
8. Projekte und Themen mit konkreten Segmentbelegen vorschlagen;
9. einen geprüften Sitzungsplan atomar als lokales Manifest bestätigen.

Noch nicht implementiert sind:

- manuelle Projektkorrekturen beim Bestätigen einer Sitzung;
- der Export bestätigter Manifeste in einen Drive-Ausgang für Atlas;
- Importquittungen aus Atlas;
- ein vollständiger Projektkatalog aus dem Z-System.

## Empfohlene Systemgrenze

```text
Atlas
  │
  ├── exportiert Projektkatalog (JSON)
  ▼
Transkriptionspipeline
  ├── erstellt Routingvorschlag
  ├── übernimmt ausdrückliche manuelle Korrekturen
  ├── bestätigt unveränderliches Sitzungsmanifest
  └── veröffentlicht Übergabepaket nach Drive
             │
             ▼
      AudioRec Project Outbox
             │
             ▼
Atlas importiert idempotent und schreibt eine Quittung
```

Atlas bleibt Eigentümer der Projektidentitäten und Projektbeziehungen. Die
Transkriptionspipeline bleibt Eigentümerin der Audio-, Transkript-, Sitzungs-
und Segmentidentitäten. Die Drive-Übergabe verbindet beide Systeme über stabile
IDs statt über veränderliche Anzeigenamen.

## Dateiformate

### Primärformat: JSON

Für die maschinenlesbare Schnittstelle wird UTF-8-JSON empfohlen.

Gründe:

- Projekte besitzen Listen von Aliasen und Beziehungen;
- ein Projekt kann einem Meta-Projekt angehören;
- eine Sitzung kann mehreren Projekten mit unterschiedlichen Geltungsbereichen
  zugeordnet sein;
- JSON lässt sich versionieren, validieren und kanonisch hashen;
- Python und die Atlas-App können es ohne verlustreiche Umformung lesen.

Empfohlene Dateien:

- `atlas-project-catalog.v1.json`: konkreter Export aus Atlas;
- `atlas-project-catalog.schema.v1.json`: JSON Schema zur Validierung;
- `delivery.json`: Manifest eines von der Pipeline veröffentlichten Pakets;
- `receipt.json`: Importquittung von Atlas.

### Ergänzende Formate

- **Markdown:** technische Übergaben, Änderungsberichte und menschliche Prüfung.
- **CSV:** optionale flache Kontrollansicht der Projektliste. Nicht als
  verbindliche Schnittstelle, da Listen und Beziehungen darin nur umständlich
  abbildbar sind.
- **HTML:** für eine spätere Prüfansicht geeignet, aber nicht als Datenaustausch.

## Vorgeschlagenes Atlas-Projektkatalog-Schema

Beispiel:

```json
{
  "schema_version": 1,
  "export_type": "z_system_project_catalog",
  "catalog_id": "z-system-main",
  "catalog_revision": "2026-09-21T12:00:00Z",
  "exported_at": "2026-09-21T12:00:00Z",
  "source": {
    "application": "Atlas",
    "instance_id": "atlas-primary"
  },
  "projects": [
    {
      "project_id": "zsys-project-z-system",
      "name": "Z-System",
      "status": "active",
      "kind": "meta_project",
      "aliases": [],
      "relations": [],
      "routing": {
        "enabled": true,
        "terms": ["Z-System"],
        "exact_terms": []
      }
    },
    {
      "project_id": "zsys-project-nachtwaechter",
      "name": "Nachtwächterprojekt",
      "status": "active",
      "kind": "tool",
      "aliases": ["Nachtwächter-App", "Nachtwächterprojekt"],
      "relations": [
        {
          "type": "part_of",
          "project_id": "zsys-project-z-system"
        }
      ],
      "routing": {
        "enabled": true,
        "terms": ["Nachtwächterprojekt", "Nachtwächter-App"],
        "exact_terms": []
      }
    }
  ]
}
```

### Bedeutung der Felder

| Feld | Bedeutung |
|---|---|
| `schema_version` | Version der Austauschstruktur, unabhängig von der App-Version |
| `catalog_id` | stabile Identität des Katalogs |
| `catalog_revision` | bei jeder fachlichen Änderung neue, vergleichbare Revision |
| `project_id` | unveränderliche, maschinenlesbare Projekt-ID; niemals aus dem Namen ableiten |
| `name` | aktueller Anzeigename |
| `status` | vorgeschlagen: `active`, `paused`, `archived` |
| `kind` | vorgeschlagen: `meta_project`, `project`, `app`, `tool`, `topic_area` |
| `aliases` | alternative Projektnamen, nicht automatisch ASR-Korrekturen |
| `relations` | gerichtete Beziehungen über Projekt-IDs, zunächst mindestens `part_of` |
| `routing.enabled` | ob automatische Vorschläge für dieses Projekt erlaubt sind |
| `routing.terms` | vollständiges kuratiertes Vokabular; Kontext- und Stützsignale |
| `routing.exact_terms` | Teilmenge sicherer Einzelbegriffe, die allein routen dürfen und strikt wortgrenzengebunden sind |

Nicht in den Atlas-Katalog gehören kurzlebige Zustände wie „aktuelles
Arbeitsprojekt“. `default_projects` und `active_projects` bleiben zunächst in
der lokalen Routingkonfiguration der Transkriptionspipeline. Ebenso bleiben
ASR-Hotwords und sichere Textersetzungen im geprüften Projektglossar, weil ein
Projektalias nicht automatisch einen Transkriptionsfehler darstellt.

`routing.exact_terms` erfüllt zwei voneinander unterscheidbare Bedingungen
gleichzeitig: Ein Treffer ist allein als Routingbeleg ausreichend, und der
Begriff muss als vollständiges Wort beziehungsweise Token vorkommen. So darf
beispielsweise `z01` nicht in `z015` und `uia` nicht in `guiabschnitt` treffen.
Mehrwortbegriffe und Aliasse stehen ausschließlich in `routing.terms`. Sie sind
starke Stützsignale; ihre spätere Gewichtung liegt bei der Pipeline und wird
nicht mit der Wortgrenzenprüfung vermischt.

### Mindestanforderungen an den Atlas-Export

- `project_id` ist eindeutig und wird nach einer Umbenennung beibehalten;
- Namen, Aliasse und Routingbegriffe enthalten keinen leeren Text;
- alle in `relations` referenzierten IDs kommen im selben Export vor;
- IDs archivierter Projekte werden nicht wiederverwendet;
- `exact_terms` ist eine Teilmenge von `terms`;
- Zeitangaben sind ISO 8601 in UTC;
- der komplette Export wird atomar ersetzt oder unter einer neuen Revision
  veröffentlicht;
- mehrdeutige Aliasse werden entweder abgelehnt oder ausdrücklich als
  mehrdeutig gekennzeichnet.

## Vorgeschlagenes Drive-Übergabemodell

Empfohlener Root-Ordner, über seine Drive-ID konfiguriert:

```text
AudioRec Project Outbox/
├── deliveries/
│   └── <delivery_id>/
│       ├── transcripts/
│       │   ├── Aufnahme #723__<audio-drive-id>.md
│       │   └── Aufnahme #724__<audio-drive-id>.md
│       └── delivery.json
└── receipts/
    └── <delivery_id>.json
```

Die Pipeline lädt zuerst die Transkriptkopien hoch, prüft Größe und SHA256 und
schreibt `delivery.json` zuletzt. Das Manifest ist damit zugleich das
Bereitschaftssignal für Atlas. Ein Ordner ohne gültiges `delivery.json` ist
nicht importbereit.

Atlas löscht oder verschiebt keine Quelldateien. Nach einem erfolgreichen,
idempotenten Import schreibt Atlas lediglich eine Quittung nach `receipts/`.
Eine spätere Aufräumregel kann sich ausschließlich auf quittierte Pakete
beziehen.

## Vorgeschlagenes Übergabemanifest

```json
{
  "schema_version": 1,
  "manifest_type": "z_system_transcript_delivery",
  "delivery_id": "session-20260921-1138-76c03dfe:0123456789abcdef",
  "created_at": "2026-09-21T12:30:00Z",
  "project_catalog": {
    "catalog_id": "z-system-main",
    "catalog_revision": "2026-09-21T12:00:00Z"
  },
  "source_routing_manifest": {
    "manifest_key": "session-20260921-1138-76c03dfe:0123456789abcdef",
    "sha256": "<sha256-des-bestaetigten-routingplans>"
  },
  "session": {
    "session_id": "session-20260921-1138-76c03dfe",
    "started_at": "2026-09-21T11:12:13+02:00",
    "ended_at": "2026-09-21T12:40:37+02:00"
  },
  "assets": [
    {
      "asset_id": "transcript:<remote-drive-id>",
      "kind": "canonical_transcript_copy",
      "relative_path": "transcripts/Aufnahme #723__<audio-drive-id>.md",
      "canonical_drive_id": "<remote-drive-id-des-kanonischen-transkripts>",
      "source_audio_drive_id": "<audio-drive-id>",
      "sha256": "<sha256>",
      "size": 12345
    }
  ],
  "assignments": [
    {
      "project_id": "zsys-project-nachtwaechter",
      "scope": "segments",
      "reasons": ["content", "manual_confirmation"],
      "segments": [
        {
          "source_audio_drive_id": "<audio-drive-id>",
          "segment_id": "segment-000017"
        }
      ]
    },
    {
      "project_id": "zsys-project-z04-myowncents",
      "scope": "whole_session",
      "reasons": ["default"],
      "segments": []
    }
  ]
}
```

`scope` unterscheidet mindestens:

- `whole_session`: Atlas erhält die vollständige Sitzung;
- `segments`: fachlich relevant sind nur die aufgeführten Segmente. Die
  Originaltranskripte bleiben dennoch über `assets` eindeutig referenziert.

Ob Atlas bei `segments` nur einen Auszug oder zusätzlich das vollständige
Transkript importiert, ist eine Importentscheidung der Atlas-App. Das Manifest
bewahrt in beiden Fällen die genaue fachliche Begründung.

## Vorgeschlagene Importquittung

```json
{
  "schema_version": 1,
  "receipt_type": "z_system_transcript_import",
  "delivery_id": "session-20260921-1138-76c03dfe:0123456789abcdef",
  "delivery_sha256": "<sha256-von-delivery.json>",
  "imported_at": "2026-09-21T12:35:00Z",
  "atlas_instance_id": "atlas-primary",
  "result": "imported"
}
```

Eine wiederholte Verarbeitung derselben `delivery_id` mit demselben Hash ist
erfolgreich und erzeugt keine zweite Atlas-Entität. Dieselbe ID mit anderem Hash
ist ein Konflikt und darf nicht still überschrieben werden.

## Geplante Implementierungsreihenfolge

1. Atlas legt stabile Projekt-IDs fest und exportiert einen kleinen realen
   Katalog mit zunächst wenigen Projekten.
2. Die Transkriptionspipeline validiert den Export lesend gegen ein JSON Schema.
3. Manuelle Projektzugaben und -entfernungen werden als Teil der ausdrücklichen
   Sitzungsbestätigung implementiert.
4. Mit einem realen Spaziergang wird das erste vollständige Routingmanifest
   bestätigt.
5. Ein Exporter erzeugt lokal zunächst nur einen Dry-Run des Übergabepakets.
6. Nach Prüfung veröffentlicht der Exporter Nutzdaten und zuletzt
   `delivery.json` in den über Drive-ID festgelegten Outbox-Ordner.
7. Atlas importiert ein Testpaket idempotent und schreibt eine Quittung.
8. Erst nach diesem End-to-End-Test wird die Übergabe automatisiert.

## Noch abzustimmende Fragen

1. Welche stabilen IDs besitzt Atlas bereits, und dürfen sie außerhalb der App
   als öffentliche technische Schlüssel verwendet werden?
2. Können Projekte mehreren Meta-Projekten angehören oder genügt zunächst
   genau eine `part_of`-Beziehung?
3. Soll Atlas bei `segments` nur relevante Auszüge oder stets zusätzlich das
   vollständige Transkript importieren?
4. Wer pflegt Routingbegriffe und Aliasse fachlich: Atlas, das
   Transkriptionsprojekt oder beide über klar getrennte Felder?
5. Wo liegt der per Drive-ID festgelegte Outbox-Root?
6. Wie lange sollen quittierte Übergabepakete auf Drive aufbewahrt werden?

## Sicherheits- und Konsistenzregeln

- Kanonische Transkripte werden niemals verändert oder verschoben.
- Ordnernamen und Anzeigenamen sind keine Identitäten.
- Nur ausdrücklich bestätigte Sitzungsmanifeste dürfen exportiert werden.
- Nutzdaten werden vor Veröffentlichung des Bereitschaftsmanifests per SHA256
  geprüft.
- Veröffentlichungen und Importe sind idempotent.
- Konflikte führen zu einem sichtbaren Fehler statt zu Überschreiben.
- Die erste Version löscht auf Drive nichts automatisch.
