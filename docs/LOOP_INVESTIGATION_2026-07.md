# Untersuchung: Whisper-Loops & „verlorener Inhalt" — Juli 2026

**Anlass:** Verdacht, dass Whisper-Wiederholungsschleifen („Loops") echten
gesprochenen Inhalt verdecken, der sich durch Neu-Transkription heben ließe.
**Ergebnis vorweg:** Kein belegbarer Fall gefunden. Eine Recovery-/
Neu-Transkriptions-Funktion ist durch die Messdaten **nicht gerechtfertigt.**

Diese Notiz hält den Befund fest, damit die Frage nicht in einem halben Jahr
erneut aufgerollt wird.

---

## 1. Kernbefunde

- **Loops sind additiver Müll, kein Deckel über verlorenem Inhalt.** Der
  stärkste denkbare Kandidat — Aufnahme #402, ein Loop (4× „Oder vielleicht
  auch irgendwo in der Bank.") über **bestätigt normal ausgesteuerter**
  Sprache (Loop-Region −23,0 dBFS) — gab beim Re-Decode **nichts Verborgenes**
  frei: der Text rundherum ist vollständig, der Loop war zusätzlicher Text.
  Wenn dort nichts liegt, liegt anderswo erst recht nichts.

- **Loops sind stochastisch.** #402 mit identischen Settings (medium, VAD an)
  neu transkribiert → der Loop reproduzierte sich **nicht**. Ein erneuter
  Lauf ist das einfachste „Reparatur"-Mittel; eine dedizierte Recovery-Logik
  braucht es dafür nicht.

- **Near-Zero-WPM korreliert mit niedrigem Aufnahmepegel = Quellproblem.**
  Sehr wortarme Transkripte (< 10 Wörter/Minute) gehören durchweg zu sehr
  leisen Aufnahmen (RMS −26 bis −62 dBFS). Beispiel: 66 min Audio → 5 Wörter
  (VAD an) bzw. Halluzinations-Loop (VAD aus). Das ist ein **Aufnahmefehler**
  (Mikrofon/Abstand/Pegel), kein Software-Bug. Selbst +15 dB Anhebung
  erzeugte nur andere Halluzinationen — es ist keine leise *Sprache*, sondern
  Stille/Rauschen.

- **Die VAD-Discard-Hypothese wurde widerlegt** (für den geprüften Fall
  #281): lokal mit `vad_filter=True` → 3 Wörter, mit `vad_filter=False` →
  10 Wörter. Ohne VAD kommt genauso wenig — VAD verwirft hier keinen echten
  Inhalt, es ist schlicht keiner da.

- **Die Archiv-Loops stammen fast alle aus der Cloud, nicht aus der lokalen
  Pipeline.** Von 39 loop-behafteten Einzeltranskripten (mit Audio) kamen nur
  **3 vom lokalen faster-whisper** (alle ≤ 4×, alle additiv über intaktem
  Text); die schweren Loops (14×–144×) sind OpenAI-Modelle
  (whisper-1 / gpt-4o-transcribe) bzw. alte header-lose OpenAI-Läufe.

---

## 2. Vorgehen (reproduzierbar)

**Stufe A — WPM-Sieb** über 251 Transkripte mit auffindbarem Audio
(Wortzahl / Audiodauer): Median 101 WPM; Ausreißer nach unten und
Near-Zero-Fälle gemeldet, je mit Median-RMS. WPM ist ein **Sieb zur
Kandidatensuche, kein Beweis** — sie schwankt natürlich stark (Geh-/
Denkpausen).

**Stufe B — gezielte RMS-Prüfung** des stärksten Loop-Verdachts (#402:
normaler Pegel → echter Decoder-Loop über Sprache) und eines Near-Zero-Falls
(#281: niedriger Pegel, VAD an/aus-Vergleich).

**Stufe C — Lücken-/Deckungsanalyse** war **nicht nötig**: Stufe B fand
keinen maskierten Inhalt, also gab es nichts zu decken.

Die hier verwendete **RMS-/Pegel-Messmethodik** (RMS im Fenster vs. Median
der Gesamtaufnahme) ist zugleich der erste Baustein für das geplante
**Audioanalyse-Modul** (Stille-/Pegelanalyse langer Aufnahmen, siehe
Konzeptnotiz vom 30.06.) — der Ansatz bleibt damit erhalten, ohne dass wir
das Investigations-Skript mitschleppen.

**Korpusweiter Loop-Scan** (statt Stichprobe) mit `eval/loop_check.py` über
alle Transkripte schloss die Beweislücke: Loops sind fast ausschließlich ein
Cloud-Phänomen; die 3 lokalen Fälle sind harmlos und additiv.

---

## 3. Werkzeuge & Code (dauerhaft im Repo)

- **`eval/loop_check.py`** — WORT-basierte Loop-Erkennung (`find_repeats`,
  `phrase_repetition`) plus CLI (Einzeldatei oder `--scan <ordner>`).
  Bewusst wortbasiert: eine **satzbasierte** Prüfung übersieht In-Satz-Loops
  („heute heute heute", oder Phrasen, die die Satzgrenzen sprengen).
  Hinweis: `--scan output` zählt auch Aggregate (Sammel-/Konsolidat-Dateien)
  mit, die Loops ihrer Mitglieder erben — für eine saubere Zählung
  Einzeltranskripte betrachten.
- **`test/test_loop_check.py`** — Regressionstest gegen die zwei bekannten
  Fälle (Mai 95×, #402 4×) plus Floskel-Ausschluss.
- **`providers/local_provider.py`** — Decoder-Parameter sind jetzt
  konfigurierbar (`condition_on_previous_text`, `repetition_penalty`,
  `compression_ratio_threshold`, `temperature`). **Defaults unverändert**
  (u. a. `condition_on_previous_text=True`).

### Zu `condition_on_previous_text`

`condition_on_previous_text=False` durchbricht Loops (Mai-Datei, VAD aus:
Redundanz 96,8 % → 23,5 %), **kostet aber Kohärenz** auf sauberen Aufnahmen
(führt selbst eine kleine Wort-Loop-Verschlechterung ein). Nutzen nur an
Dateien ohne rettbaren Inhalt belegt, Schaden auf gutem Material real →
**Default bleibt `True`**, der Parameter bleibt als Hebel verfügbar.
`no_repeat_ngram_size` bewusst **nicht** aktivieren — der Sprecher wiederholt
sich absichtlich; ein harter n-Gramm-Block würde echte Inhalte zerstören.

---

## 4. Empfehlung

1. **Keine Recovery-Funktion bauen** — nicht durch Daten gerechtfertigt.
2. Bei einem gemeldeten Loop das betroffene Transkript **einfach neu laufen
   lassen** (Loops sind stochastisch).
3. **Sehr leise Aufnahmen** (niedriger Pegel / Near-Zero-WPM) als
   **Quellproblem** behandeln (Aufnahmequalität), nicht als Modell-/
   Software-Fehler.
