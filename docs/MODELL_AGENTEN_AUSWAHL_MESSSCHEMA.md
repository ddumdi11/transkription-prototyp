# Messschema für Modell- und Agenteneinsätze

**Status:** Entscheidungs- und Schnittstellenentwurf v0.1  
**Stand:** 8. Oktober 2026  
**Ursprung:** Auswertung der Aufnahmen #792 und #795

## Ziel

Die Auswahl eines Modells oder Agenten soll nicht allein nach Preis, Größe oder
gefühlter Qualität erfolgen. Entscheidend ist, welches Werkzeug eine konkrete
Aufgabe mit vertretbarem Gesamtaufwand zu einem akzeptierten Ergebnis bringt.

v0.1 erfasst Einsätze zunächst manuell und vergleichbar. Sie soll insbesondere
folgende Fragen beantworten:

- Welches Modell eignet sich für welche Aufgabenklasse?
- Wann spart ein kleines Modell tatsächlich Zeit und Kontingent?
- Wann wird ein zunächst schnelles Ergebnis durch Nachkorrekturen teuer?
- Wie gut lässt sich ein unterbrochener Lauf fortsetzen?
- Welche Aufgaben sollten lokal, in einem spezialisierten Werkzeug oder mit
  einem stärkeren allgemeinen Modell bearbeitet werden?

Das Messschema ist projektübergreifend. Es verändert weder die
Transkriptionspipeline noch Konten, Kontingente oder bestehende Projektstände.

## Leitgröße: akzeptiertes Ergebnis

Die reine Laufzeit ist keine ausreichende Erfolgskennzahl. Gemessen wird die
Zeit bis zu einem Ergebnis, das der Nutzer tatsächlich akzeptiert:

```text
Gesamtaufwand = Wartezeit + eigene Arbeitszeit + Nachkorrekturzeit
```

Ein Lauf in vier Minuten kann schlechter abschneiden als ein längerer Lauf,
wenn anschließend mehrere Korrekturrunden erforderlich sind. Umgekehrt kann
ein kleines Modell für ein eng begrenztes Blitzprojekt optimal sein.

## Abgrenzung von v0.1

v0.1 ist absichtlich klein:

- ein Datensatz pro beobachtetem Einsatz,
- manuelle Erfassung unmittelbar nach dem Lauf,
- keine automatische Anmeldung oder Auslesung fremder Dienste,
- keine Speicherung vollständiger Prompts oder vertraulicher Inhalte,
- keine automatische Rangliste nach einzelnen Versuchen,
- keine produktive Kopplung an Atlas oder die Transkriptionspipeline.

Automatisierte Messwerte können später ergänzt werden, wenn ein Dienst sie über
eine vorgesehene Schnittstelle zuverlässig bereitstellt.

## Vorgeschlagener Datensatz

Das spätere Austauschformat sollte UTF-8-JSON oder JSONL sein. JSONL eignet
sich besonders für ein append-only Messjournal, weil ein neuer Lauf als genau
eine Zeile ergänzt werden kann.

```json
{
  "schema_version": 1,
  "record_type": "model_agent_run",
  "run_id": "018f-example-uuid",
  "observed_at": "2026-10-08T10:30:00Z",
  "project_id": "gedankenfaeden",
  "task": {
    "task_ref": "optional-stable-reference",
    "task_class": "analysis",
    "summary": "Fehlerprotokoll eines Gedankenfaden-Laufs auswerten",
    "sensitivity": "private",
    "expected_result": "actionable_recommendation"
  },
  "execution": {
    "application": "codex",
    "provider": "openai",
    "model": "unknown",
    "mode": "agent",
    "settings_profile": "default",
    "started_at": null,
    "completed_at": null,
    "wall_seconds": null,
    "cold_start": null
  },
  "measurement_sources": {
    "timing": "local_measurement",
    "tokens": "unknown",
    "cost": "unknown",
    "quota": "ui_observation"
  },
  "usage": {
    "input_tokens": null,
    "cached_input_tokens": null,
    "output_tokens": null,
    "cost_amount": null,
    "cost_currency": null,
    "quota_before_percent": null,
    "quota_after_percent": null
  },
  "outcome": {
    "status": "completed",
    "accepted": true,
    "quality_rating": 4,
    "correction_rounds": 2,
    "correction_minutes": null,
    "result_ref": null
  },
  "interruption": null,
  "tags": ["blitzprojekt", "gedankenfaeden"],
  "notes": "Mehrere unterschiedliche Fehlerarten; Request-Logging ergänzt."
}
```

`null` ist ausdrücklich zulässig, wenn ein Wert nicht zuverlässig beobachtet
werden konnte. Unbekannte Werte werden nicht geschätzt und nicht als `0`
gespeichert.

## Pflichtinformationen

Für einen verwertbaren v0.1-Datensatz genügen:

- stabile `run_id`,
- Beobachtungszeitpunkt,
- kurze, nicht vertrauliche Aufgabenbeschreibung,
- Aufgabenklasse,
- Anwendung sowie Modell, soweit bekannt,
- Abschlussstatus,
- Akzeptanzentscheidung,
- Zahl der Korrekturrunden,
- bekannte Unterbrechungen und
- Herkunft jedes Verbrauchswertes.

`project_id` verwendet nach der späteren Kopplung ausschließlich eine stabile
ID aus dem Atlas-Projektkatalog. Bis zum aktualisierten Export darf das Feld
fehlen; freie Projektnamen gehören dann vorläufig in `tags` oder `notes`.

## Aufgabenklassen v0.1

Die Liste bleibt bewusst überschaubar:

- `quick_task`: eng begrenztes Ergebnis, möglichst ohne Nacharbeit,
- `analysis`: Material verstehen, ordnen und bewerten,
- `research`: Quellen suchen, prüfen und zusammenführen,
- `coding`: Code erzeugen, ändern oder untersuchen,
- `review`: vorhandenes Ergebnis gezielt kontrollieren,
- `planning`: Vorgehen oder Architektur entwickeln,
- `writing`: einen verwendbaren Text erstellen,
- `multimodal`: Bild, Audio oder andere Medien verarbeiten,
- `other`: nur mit erklärender Notiz.

Mehrere Aspekte werden nicht durch mehrere Klassen ausgedrückt, sondern über
`tags` ergänzt. Dadurch bleibt die Auswertung vergleichbar.

## Ergebnis- und Qualitätswerte

### Abschlussstatus

- `completed`: Lauf lieferte ein Ergebnis,
- `interrupted`: Lauf endete vor einem Ergebnis,
- `failed`: technischer oder fachlicher Fehlschlag,
- `abandoned`: bewusst abgebrochen,
- `partial`: verwertbarer Zwischenstand, aber Ziel nicht erreicht.

### Qualitätsbewertung

Die subjektive Skala `1` bis `5` bewertet das tatsächlich erhaltene Ergebnis:

1. unbrauchbar,
2. nur mit grundlegender Neuerstellung nutzbar,
3. brauchbar nach deutlicher Korrektur,
4. gut mit kleinen Korrekturen,
5. direkt akzeptiert.

Die Zahl ersetzt nicht `accepted`, `correction_rounds` oder eine kurze Notiz.
Gerade die Kombination macht spätere Vergleiche verständlich.

## Verbrauch und Messquellen

Jeder Zahlenwert benötigt eine Herkunft:

- `api`: vom Anbieter maschinenlesbar geliefert,
- `ui_observation`: in einer Oberfläche abgelesen,
- `local_measurement`: lokal gemessene Laufzeit oder Ressource,
- `estimated`: begründete Näherung,
- `unknown`: nicht verfügbar.

UI-Prozentwerte sind grob und dürfen nicht wie exakte Tokenzahlen behandelt
werden. Eine Verschiebung des angezeigten Rücksetzdatums ist ein beobachtbares
Ereignis, aber kein Beweis für die interne Berechnungsregel eines Anbieters.

## Unterbrechungen und Wiederaufnahme

Für unterbrochene Läufe ersetzt `interruption: null` ein Objekt:

```json
{
  "kind": "quota",
  "occurred_at": "2026-10-07T12:00:00Z",
  "message_summary": "Nutzungslimit erreicht",
  "announced_reset_at": "2026-10-14T00:00:00Z",
  "resume_attempted": true,
  "resume_instruction": "Bitte fortsetzen; der Lauf wurde unterbrochen.",
  "context_reconstruction_rating": 4,
  "lost_work": false
}
```

Der Wiederaufnahmehinweis darf gespeichert werden, wenn er keine vertraulichen
Inhalte enthält. Vollständige Prompts werden standardmäßig nicht in das
Messjournal kopiert.

## Abgeleitete Kennzahlen

Erst nach mehreren vergleichbaren Läufen werden Kennzahlen gebildet:

- Zeit bis zum akzeptierten Ergebnis,
- mittlere Korrekturrunden je Aufgabenklasse,
- Annahmequote ohne Nachkorrektur,
- Fehl- und Unterbrechungsquote,
- Wiederaufnahmequalität,
- Kosten beziehungsweise Kontingentverbrauch pro akzeptiertem Ergebnis,
- Anteil unbekannter Messwerte.

Eine einzelne erfolgreiche oder schlechte Ausführung begründet keine allgemeine
Modellempfehlung. Für eine vorsichtige Tendenz sollten mindestens drei ähnlich
gelagerte Einsätze vorliegen.

## Entscheidungsprofile statt Gesamtrangliste

Es soll keinen universellen „besten Agenten“ geben. Spätere Empfehlungen nutzen
unterschiedliche Gewichtungen:

- **Blitzprojekt:** geringe Wartezeit und wenig Nacharbeit,
- **gründliche Analyse:** Ergebnisqualität und Quellenarbeit,
- **Coding:** Korrektheit, Tests und sichere Änderungen,
- **sensibler Inhalt:** lokale Verarbeitung und Datenminimierung,
- **knappes Kontingent:** Verhältnis von Akzeptanz zu Verbrauch,
- **lange Aufgabe:** Stabilität und Wiederaufnahmefähigkeit.

So kann dasselbe Modell für eine Aufgabenklasse empfohlen und für eine andere
zurückgestellt werden.

## Erste reale Beobachtungen

Die Aufnahmen #792 und #795 liefern zwei noch unvollständige Startfälle:

1. **Gedankenfäden:** mehrere Nachkorrekturen und unterschiedliche Fehlerarten;
   Request- und Fehlerlogging wurden als Folgemaßnahme ergänzt. Modell,
   Verbrauch und genaue Korrekturzeit sind noch unbekannt.
2. **Schneller Zeitentest:** Ergebnis nach ungefähr vier Minuten mit kleinster
   beziehungsweise standardmäßig gewählter Einstellung. Modellname,
   Verbrauch und abschließende Qualitätsbewertung müssen noch ergänzt werden.

Bei einer späteren Erfassung bleiben die unbekannten Felder zunächst `null`,
bis sie zuverlässig aus der jeweiligen Anwendung abgelesen wurden.

## Abstimmung mit dem Z-System

Die Abstimmung vom 9. Oktober 2026 hat die vorläufige Zuordnung geklärt:

- `modell-agenten-einsatz` ist die stabile Projekt-ID für das
  projektübergreifende Vorhaben „Modell- und Agenteneinsatz“. Das Z-System ist
  dessen Eigentümer; die Transkriptionspipeline kann später Beobachtungen
  liefern.
- Blitzprojekte besitzt noch keine stabile Projekt-ID und ist deshalb noch kein
  Routing-Ziel.
- Gedankenfäden bleibt vorerst ein Chat beziehungsweise Untervorhaben unter
  Blitzprojekte und erhält noch keine eigene Projekt-ID. Bis dahin kann es nur
  über Tags, Notizen oder eine stabile Aufgabenreferenz bezeichnet werden.
- Vorhabenübergreifende Verbindungen entstehen in Katalog v1 über die
  N:M-Zuordnung Chat↔Vorhaben, nicht über Projekt-Relationen.

Der zugehörige Atlas-Katalog wurde direkt aus
`AudioRec Project Outbox/catalog/atlas-project-catalog.v1.json` validiert:

```text
exported_at: 2026-10-09T18:56:18Z
projects: 45
catalog_hash: sha256:e4fb06f8de669934eae1f3d2652dc192659abf20a927a2b23b8ac4a300e0a9ea
```

Die Outbox ist bereits der Drive-synchronisierte Übergabekanal. Ein neuer
Katalog wird nicht noch einmal separat übergeben: Sein neuer `catalog_hash` ist
das Änderungssignal, anhand dessen die empfangende Seite genau die aus der
Outbox synchronisierte oder abgerufene Datei validiert. Der Abruf geschieht auf
Pipeline-Seite derzeit noch ausdrücklich; eine automatische rclone-Anbindung
für den Katalog ist noch nicht implementiert.

Noch offen sind Ablageort und Besitzer des append-only Messjournals, ein
optionaler Exporttyp für zusammengefasste Empfehlungen sowie der Umgang mit
archivierten oder umbenannten Projekten.

## Nächste Schritte

1. Drei bis fünf reale Einsätze für `modell-agenten-einsatz` manuell nach
   diesem Schema erfassen.
2. Prüfen, welche Felder tatsächlich zuverlässig verfügbar sind.
3. Ablage und Eigentümer des Messjournals mit dem Z-System festlegen.
4. Erst danach ein formales JSON-Schema und einen Validator festlegen.
5. Automatisierung nur für stabile, vom jeweiligen Dienst vorgesehene
   Messschnittstellen entwickeln.
