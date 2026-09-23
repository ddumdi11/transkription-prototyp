---
Rolle: Z02-Entscheidung zur Schnittstelle Atlas ↔ Transkriptionspipeline
Datum: 2026-09-21
Status: bestätigt
Grundlage: Entscheidungsvorlage und projektübergreifende Abstimmung vom 21.09.2026
---

# Entscheidung: Schnittstelle Atlas ↔ Transkriptionspipeline

Die vorgeschlagene Schnittstelle und die Trennung der Vorhaben
`projektverwaltung-steuerung` und `z-system` werden bestätigt.

## Entscheidungen E1–E5

- [x] **E1 – Projektidentität:** `project_id` entspricht dem stabilen
  Vorhaben-Slug ohne Präfix. Container und Sentinels werden nicht exportiert.
  Der Statusvorrat enthält `active`, `paused`, `archived` und `candidate`.

- [x] **E2 – Atlas-Entität:** Jedes kanonische Transkript wird höchstens eine
  `conversations`-Zeile mit `source = "audiorec"`,
  `external_id = canonical_drive_id` und dem SHA256 als `content_hash`.
  Projektzuordnungen erfolgen N:M über `conv_project`; `scope`, `reasons` und
  `segments` bleiben als Begründungsmetadaten erhalten.

- [x] **E3 – Vollständige Quelle:** Auch bei `scope = "segments"` importiert
  Atlas das vollständige Transkript. Segmentangaben begründen die Zuordnung,
  ersetzen aber niemals den vollständigen Rohbestand.

- [x] **E4 – Vorläufige Standardvorhaben der Pipeline:**
  `projektverwaltung-steuerung` und `selbstregulation`. Die Entscheidung wird
  nach dem ersten realen Katalogexport und End-to-End-Test erneut geprüft.
  `Z04 - MyOwn2Cents` ist ein Arbeitsort beziehungsweise Containeralias und
  keine technische Vorhaben-ID. Für `selbstregulation` werden sowohl
  „Selbstregulation statt Selbstverbesserung“ als auch „Selbstregulation statt
  Selbstoptimierung“ als Aliasse geführt.

- [x] **E4a – Zwei getrennte Vorhaben:**
  - `projektverwaltung-steuerung` umfasst den operativen Fluss über Z01–Z04;
  - `z-system` umfasst die technische und konzeptionelle Infrastruktur wie
    Atlas, Nachtwächter, Cockpit und Herbert.

  Z01, Z02, Z03 und Z04 werden dem Vorhaben
  `projektverwaltung-steuerung` zugeordnet. Die Infrastruktur-Container werden
  `z-system` zugeordnet. Eine spätere Meta-Beziehung zwischen beiden Vorhaben
  bleibt möglich, ist aber nicht Bestandteil des Katalogschemas v1.

- [x] **E5 – Outbox:** Auf der obersten Drive-Ebene wird
  `AudioRec Project Outbox` angelegt. Der Ordner wird auf beiden Seiten über
  seine Drive-ID beziehungsweise den lokal synchronisierten Pfad konfiguriert.
  Version 1 löscht oder verschiebt keine Übergabepakete automatisch.

## Bestätigter Bearbeitungsfluss

Die automatische Vorhabenzuordnung und der bestehende Z-Arbeitsfluss erfüllen
unterschiedliche Aufgaben und bleiben miteinander verbunden:

```text
Transkriptionspipeline
  │  vollständiges Transkript + bestätigte Zuordnungsbelege
  ▼
Z03 – Projektquellen
  │  unveränderter Rohbestand / Quellenindex
  ▼
Z04 – MyOwn2Cents
  │  Analyse, Verdichtung und Einordnung
  │  Übergabenotiz mit Quellen- und Sitzungsbezug
  ▼
Z01 – Zentrale Projektverwaltung
  │  Vorhaben-/Registerzuordnung und laufende Pflege
  │
  ├── Routinefall: direkt in Register und Vorhaben einarbeiten
  │
  └── Entscheidungsfall: Übergabenotiz
         ▼
       Z02 – Zentrale Projektsteuerung
         Priorisierung, Konfliktklärung und Grundsatzentscheidung
```

Die Zuordnung eines importierten Transkripts zu
`projektverwaltung-steuerung` bedeutet nicht, dass die Quelle Z04 oder Z01
überspringt. Sie kennzeichnet den fachlichen Zusammenhang in Atlas. Der
Bearbeitungsstatus ergibt sich weiterhin aus dem jeweiligen Arbeitsort und den
dort erzeugten Übergabenotizen.

## Praktische Präzisierung

Z01 erzeugt nicht für jede routinemäßige Änderung eine Übergabenotiz an Z02.
Eine Weitergabe an Z02 ist erforderlich, wenn mindestens einer der folgenden
Fälle vorliegt:

- eine Prioritäts- oder Reihenfolgeentscheidung;
- ein Konflikt zwischen Vorhaben oder Ressourcen;
- eine neue Grundsatzentscheidung;
- eine unklare oder strittige Vorhabenzuordnung;
- eine Änderung der Systemstruktur oder verbindlichen Regeln.

Reine Registerpflege, zusätzliche Quellen und bereits entschiedene
Standardabläufe bearbeitet Z01 ohne zusätzliche Z02-Notiz. Dadurch bleibt die
Rollenfolge erhalten, ohne unnötige Übergaben zu produzieren.

## Daten- und Herkunftsregeln

- Die kanonische Markdown-Datei bleibt unverändert.
- Z03 führt den vollständigen Quellenbestand.
- Z04- und Z01-Notizen sind abgeleitete Artefakte und referenzieren mindestens
  `delivery_id`, `session_id` und die betroffenen `canonical_drive_id`-Werte.
- Eine Sitzung mit mehreren Aufnahmen erzeugt je kanonischem Transkript eine
  Atlas-Conversation.
- `scope = "whole_session"` gilt für alle Transkripte der Sitzung.
- `scope = "segments"` gilt nur für Transkripte mit den genannten
  Segmentbelegen; Atlas importiert dennoch deren vollständigen Inhalt.
- Identische Wiederholungen sind idempotent. Abweichende Inhalte unter
  derselben Identität werden als Konflikt behandelt.

## Routingbegriffe aus Atlas

- `routing.terms` ist das vollständige kuratierte Vokabular und liefert
  Kontext- sowie Stützsignale.
- `routing.exact_terms` ist eine Teilmenge sicherer Einzelbegriffe, die allein
  einen Routingtreffer begründen dürfen.
- Jeder Treffer aus `exact_terms` ist zusätzlich strikt wortgrenzengebunden;
  Teilstrings wie `z01` in `z015` oder `uia` in `guiabschnitt` zählen nicht.
- Mehrwortbegriffe und Aliasse stehen nur in `terms`. Sie sind starke
  Stützsignale, aber kein automatischer Einzelbeweis.
- Die drei noch nicht slugförmigen Kandidaten werden vor der ersten
  produktiven Lieferung entweder triagiert oder mit endgültigen stabilen
  Slugs versehen.

## Outbox-Struktur v1

```text
AudioRec Project Outbox/
├── catalog/
│   └── atlas-project-catalog.v1.json
├── deliveries/
│   └── <delivery_id>/
│       ├── transcripts/
│       └── delivery.json
└── receipts/
    └── <delivery_id>.json
```

## Freigegebene nächste Schritte

1. Atlas legt beziehungsweise ordnet die beiden Vorhaben und ihre Container zu.
2. Atlas exportiert den ersten kleinen Projektkatalog.
3. Beide Projekte stimmen das JSON Schema und die Kataloghash-Testvektoren ab.
4. Die Pipeline implementiert Katalogvalidierung und Slug-basierte Zuordnung.
5. Die Pipeline ergänzt manuelle Projektzugaben und -entfernungen bei der
   Sitzungsbestätigung.
6. Pipeline-Exporter und Atlas-Importer werden zunächst als Dry-Run gegen
   gemeinsame Beispieldateien entwickelt.
7. Nach Anlage und Konfiguration der Outbox erfolgt ein realer
   End-to-End-Test mit Importquittung.
8. Erst nach erfolgreicher Wiederholung wird der Ablauf automatisiert.
