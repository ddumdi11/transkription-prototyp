# Bug Report / Workflow Issue

---

**ID:** BUG-TRANSCRIBE-001
**Datum:** 2026-06-08
**Entdeckt in:** MyOwn2Cents / Z04 Transkriptions-Workflow
**Typ:** Workflow-Designfehler (kein Code-Bug im engeren Sinne)
**Schweregrad:** Mittel – führt zu Datenverlust auf Inhaltsebene

---

## Problembeschreibung

Einzelne Audio-Aufnahmen werden vor der Transkription zu einer zusammengeführten Datei verbunden. Dadurch gehen Aufnahme-Grenzen, Metadaten und thematische Abschnittszuordnungen verloren. Inhalte einzelner Aufnahmen können im Sammeltranskript verschwinden, untergewichtet werden oder nicht mehr einer konkreten Quelle zuordenbar sein.

**Konkretes Symptom:** Der Inhalt aus Aufnahme #264 (ÖPNV-Kleinbus-Impuls) war in einem Zusammen-Transkript aus #262–#264 nicht auffindbar, obwohl er in der Einzeltranskription von #264 klar enthalten war.

---

## Ursache

Der Workflow folgte implizit der Annahme: *„Zusammengehörige Aufnahmen sollen auch zusammen transkribiert werden."* Diese Annahme ist für den tatsächlichen Nutzungsfall nicht zutreffend.

Negative Effekte des bisherigen Ablaufs:

- Aufnahme-Nummern/-Grenzen gehen verloren
- Themenwechsel werden vom Modell geglättet oder übergangen
- Fehler in einer Aufnahme kontaminieren das gesamte Sammeltranskript
- Gezielte Rückfragen wie „Was stand in #264?" sind nicht mehr zuverlässig beantwortbar
- Modellvergleiche zwischen Transkriptionen werden unsauber

---

## Erwartetes Verhalten

Jede Aufnahme wird einzeln transkribiert und als eigene Datei mit Metadaten-Kopf gespeichert. Zusammenführung findet ausschließlich auf Textebene statt, nach der Transkription.

---

## Gewünschter Ablauf (Soll)

```text
Audio #262  →  Transkript_262.md
Audio #263  →  Transkript_263.md
Audio #264  →  Transkript_264.md

(optional, danach)
Tages_Sammeltranskript_2026-06-08.md
Tages_Analyse_2026-06-08.md
Uebergabenotiz_2026-06-08.md
```

Jede Einzeltranskript-Datei erhält einen Metadaten-Kopf, z. B.:

```markdown
# Aufnahme #264
- Datum: 2026-06-08
- Modell: gpt-4o-transcribe
- Quelle: Einzelaufnahme
- Kontext: Nach Rückkehr / ÖPNV-Beobachtung
- Status: Rohtranskript
```

---

## Ausnahme / Sonderfall

Audio-Zusammenführung vor der Transkription bleibt zulässig, wenn Aufnahmen bewusst eine einzige zusammenhängende Einheit bilden sollen (z. B. für Batch-Kostenoptimierung oder wenn das Modell für Satzanschlüsse längeren Kontext benötigt). Muss dann explizit so markiert werden.

---

**Status:** Behoben (10.06.2026)
**Priorität:** Hoch (betraf Grundstruktur des Workflows)

## Auflösung (10.06.2026)

- `transcribe.py`: `--metadata-header` schreibt den Metadaten-Kopf je Einzeltranskript
  (Aufnahme-Nr. aus Dateiname, Datum aus Dateidatum, Modell, Quelle, Kontext-Platzhalter,
  Status). `--move-processed` verschiebt Quell-Unterordner nach fehlerfreiem Lauf nach
  `processed/`.
- GUI: Workflow-Modus-Schalter — **Einzeltranskription ist der neue Standard**; das
  Zusammenfügen vor der Transkription bleibt als markierter Sonderfall wählbar.
- `--merge-transcripts`: Zusammenführung auf **Textebene** nach der Transkription —
  je Quellordner entsteht `<Ordnername>.md` aus allen Einzeltranskripten (inkl.
  Metadaten-Köpfen, sortiert nach Aufnahme-Nr.); die Einzeltranskripte bleiben
  erhalten. GUI-Checkbox im Einzelmodus, Standard: an.
- Abweichung vom Soll: Das Feld `Kontext:` wird leer angelegt (inhaltliche Einordnung
  kann nur manuell oder durch spätere LLM-Analyse erfolgen); zusätzlich Feld `Datei:`
  mit dem Original-Dateinamen.
- Noch offen (Folgeeinheit): Tages-*Analyse* und *Übergabenotiz* auf Basis des
  Sammeltranskripts.
