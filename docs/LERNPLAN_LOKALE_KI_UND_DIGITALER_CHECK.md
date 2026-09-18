# Gestufter Lernplan: lokale KI und digitaler Check

Stand: 11. September 2026

Ursprüngliches Ziel: fachliche Vorbereitung auf das Gespräch am 16. September
2026 und zugleich Aufbau eines praktisch erprobbaren Angebots für die
IT-Dienstleistungen Probephase. Die Lernstufen und Kernkarten bleiben als
fachliche Referenz erhalten.

## Lernprinzip

Die Stufen werden nacheinander bearbeitet. Jede neue Stufe setzt die wichtigen
Begriffe der vorherigen voraus und verwendet sie erneut. Ein Begriff gilt für
diesen Lernplan als verstanden, wenn er

1. in eigenen Worten erklärt,
2. an der bestehenden Transkriptionspipeline gezeigt und
3. mit Nutzen oder Risiko für einen Kunden verbunden werden kann.

Kurze Einheiten von 20 bis 30 Minuten genügen. Nach jeder Einheit wird ohne
Unterlagen eine zweiminütige mündliche Zusammenfassung aufgenommen. Unsichere
Begriffe wandern in die nächste Wiederholung, statt die gesamte Einheit zu
blockieren.

## Stufe 1 – Unverzichtbare Grundlagen

Diese Stufe beantwortet: **Was läuft wo, und welche Ressourcen braucht es?**

- **KI-Modell:** gelernte Parameter, die aus einer Eingabe eine Ausgabe
  erzeugen; noch keine vollständige Anwendung.
- **Inferenz:** die konkrete Ausführung eines trainierten Modells.
- **lokal und Cloud:** lokal bedeutet Verarbeitung auf dem eigenen Gerät;
  Cloud bedeutet Verarbeitung auf fremder Infrastruktur über ein Netz.
- **Token und Kontextfenster:** Texteinheiten und die begrenzte Menge davon,
  die ein Modell in einem Durchlauf berücksichtigen kann.
- **Prompt und System-Prompt:** Arbeitsauftrag beziehungsweise übergeordnete
  Regeln für das Modell.
- **CPU, GPU, RAM und VRAM:** Recheneinheiten und Speicher, die Geschwindigkeit
  und mögliche Modellgröße begrenzen.
- **Quantisierung:** geringere Zahlengenauigkeit verkleinert ein Modell und
  senkt seinen Ressourcenbedarf, möglicherweise auf Kosten von Qualität.
- **GGUF:** verbreitetes Dateiformat für quantisierte Sprachmodelle und ihre
  Metadaten, besonders für lokale Laufzeitumgebungen.
- **Qwen:** Modellfamilie, von der ein passendes Modell als lokaler Copilot für
  das Z-System erprobt werden soll.

### Lernkontrolle

In höchstens drei Minuten erklären: Warum kann ein quantisiertes GGUF-Modell
lokal sinnvoll sein, und welche Grenzen setzen RAM, VRAM und Kontextfenster?

## Stufe 2 – Modelle, Dienste und Werkzeuge verbinden

Diese Stufe beantwortet: **Wie wird aus einem Modell ein verwendbares System?**

- **Runtime:** Software, die ein Modell tatsächlich lädt und ausführt.
- **API:** vereinbarte Schnittstelle, über die Programme Funktionen oder
  Modelle ansprechen.
- **Provider:** Anbieter oder Dienst, der Modelle über eine einheitliche oder
  eigene Schnittstelle bereitstellt.
- **OpenRouter:** Zugangsschicht zu Modellen verschiedener Anbieter.
- **OpenCode:** Entwicklungswerkzeug, das Modelle bei Arbeiten an Code und
  Projekten einsetzen kann.
- **Tool Calling:** ein Modell fordert eine klar definierte Programmfunktion
  an; das Programm kontrolliert deren tatsächliche Ausführung.
- **Copilot und Agent:** ein Copilot unterstützt den Menschen; ein Agent führt
  innerhalb gesetzter Grenzen mehrere Schritte und Werkzeuge selbstständig aus.
- **Embeddings:** Zahlenrepräsentationen, mit denen inhaltlich ähnliche Texte
  gefunden werden können.
- **RAG:** vor einer Antwort werden passende Informationen gesucht und dem
  Modell als zusätzlicher Kontext gegeben.

### Lernkontrolle

Eine mögliche Z-System-Kette skizzieren: Benutzerauftrag → Copilot → Modell →
Werkzeug oder Wissenssuche → kontrolliertes Ergebnis. An jeder Stelle benennen,
welche Daten das eigene Gerät verlassen.

## Stufe 3 – Robuste Automatisierung

Diese Stufe beantwortet: **Wie bleibt ein Ablauf bei Unterbrechungen korrekt?**

- **Pipeline:** geordnete Folge von Verarbeitungsschritten.
- **Job:** eindeutig beschriebene Arbeitseinheit mit Status und Ergebnis.
- **Job Queue:** Warteschlange noch auszuführender Jobs.
- **persistenter Zustand:** Fortschritt wird dauerhaft gespeichert und
  übersteht Programmende oder Neustart.
- **Checkpointing:** ein bestätigter Zwischenstand ermöglicht die Fortsetzung,
  ohne alles von vorn auszuführen.
- **Idempotenz:** eine Wiederholung hat nach dem ersten Erfolg keine zusätzliche
  unerwünschte Wirkung.
- **Deduplizierung:** Drive-ID identifiziert den Upload, ein Hash den Inhalt;
  identischer Inhalt wird nicht doppelt verarbeitet.
- **Retry und Backoff:** vorübergehende Fehler werden kontrolliert und mit
  wachsendem Abstand erneut versucht.
- **Logging und Monitoring:** nachvollziehbare Ereignisse sowie ein sichtbarer
  Hinweis, wenn der normale Ablauf ausbleibt.
- **Fallback:** vorher festgelegter Ersatzweg, wenn der bevorzugte Dienst oder
  ein Modell nicht verfügbar ist.
- **Orchestrator:** koordiniert Jobs, Status, Werkzeuge und Fehlerbehandlung,
  ohne selbst zwingend die Facharbeit auszuführen.

### Praxisbeispiel

Die AudioRec-Pipeline kann als vollständiges Beispiel erklärt werden:

1. Eine Aufnahme wird anhand Drive-ID und Hash beobachtet.
2. Erst zwei unveränderte Beobachtungen machen sie bereit.
3. Der Job wird lokal bereitgestellt und transkribiert.
4. Das Ergebnis wird dauerhaft als erledigt markiert.
5. Das kanonische Transkript wird veröffentlicht und anschließend geroutet.
6. Wiederholte Timerläufe erzeugen weder zweite Transkripte noch zweite
   Veröffentlichungen.

### Lernkontrolle

Erklären, weshalb **Job Queue**, **Checkpointing** und **Idempotenz** drei
unterschiedliche Aufgaben lösen. Danach angeben, was bei abgelaufenem
rclone-Token passieren und wie der Mensch informiert werden sollte.

## Stufe 4 – Übertragung auf den digitalen Check

Diese Stufe beantwortet: **Wie wird das technische Wissen zu einem sicheren
Kundenangebot?**

Der erste digitale Check richtet sich zunächst an Privatkunden. Vorgesehener
Markttest: ungefähr eine Stunde, Einführungspreis etwa 29 Euro; ein regulärer
Stundensatz von 59 Euro bleibt eine zu prüfende Arbeitshypothese.

### Ablaufentwurf

1. Ziel, wichtigste Probleme und Zeitrahmen mit dem Kunden festlegen.
2. Geräte, Konten, Speicherorte und vorhandene Sicherungen inventarisieren.
3. Sicherheitskritisches zuerst prüfen: Updates, Kontozugänge,
   Mehrfaktor-Anmeldung, Passwortverwaltung und Wiederherstellung.
4. Dateien, Explorer-Zugriffe und Cloudspeicher nur analysieren; vor jeder
   Verschiebung oder Löschung ausdrücklich freigeben lassen.
5. Backup und Synchronisierung unterscheiden und jeweils den Ernstfall
   erklären.
6. Kleine, robuste Verbesserungen direkt erproben und dokumentieren.
7. Eine Übergabe erstellen, die der Kunde selbst verstehen, pflegen und
   rückgängig machen kann.

### Leitlinien

- Empfehlungen richten sich nach Arbeitsweise und Prioritäten des Kunden,
  nicht nur nach den eigenen Vorlieben.
- Risiken und Zielkonflikte werden transparent benannt.
- Automatisierung muss nachvollziehbar, begrenzt und möglichst idempotent sein.
- Sprachsteuerung beginnt nur mit ungefährlichen, bestätigungspflichtigen
  Aktionen; Löschen und Verschieben werden nicht allein durch unsichere
  Spracherkennung ausgelöst.
- Passwortmanager werden nicht unkoordiniert doppelt gepflegt. Vor einer
  redundanten Lösung werden führende Datenquelle, Synchronisierung, Export,
  Wiederherstellung und Konfliktbehandlung festgelegt.

### Lernkontrolle

Den digitalen Check in fünf Minuten als Kundengespräch erklären: Nutzen,
Ablauf, Grenzen, Datenschutz und das konkrete Ergebnis nach einer Stunde.

## Historischer Zeitplan bis zum 16. September 2026

Die folgenden Termine dokumentieren die damalige Vorbereitung und sind kein
aktueller Ablaufplan.

- **11. September:** Stufe 1 lesen und als Sprachnotiz erklären.
- **12. September:** Stufe 1 wiederholen, dann Stufe 2 bearbeiten.
- **13. September:** Stufen 1 und 2 kurz abrufen, Stufe 3 an der bestehenden
  AudioRec-Pipeline nachvollziehen.
- **14. September:** Stufe 4 am eigenen Windows-11-Rechner probeweise
  durchführen; nur inventarisieren und reversible Verbesserungen vornehmen.
- **15. September:** fünfminütige Darstellung des Angebots und dreiminütige
  Erklärung der lokalen KI ohne Unterlagen aufnehmen; offene Lücken gezielt
  nachlernen.
- **16. September:** nur Kernkarten wiederholen, keine neue Themenstufe öffnen.

## Kernkarten für die Kurzabfrage

1. Was unterscheiden Modell, Runtime, Provider und Anwendung?
2. Was bewirken Quantisierung und GGUF bei lokaler KI?
3. Was unterscheiden Copilot, Agent und Orchestrator?
4. Was unterscheiden Embeddings und RAG?
5. Was unterscheiden Job Queue, Checkpointing und Idempotenz?
6. Warum braucht eine robuste Pipeline persistenten Zustand, Retry und Logs?
7. Was unterscheiden Synchronisierung und Backup?
8. Wie schützt ein digitaler Check die Selbstständigkeit des Kunden?
