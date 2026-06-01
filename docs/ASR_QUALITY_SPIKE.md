# Evaluations-Protokoll: ASR-Qualitätshebel

**Rolle:** Mess- und Entscheidungsgrundlage für PO (Thorsten) + Kurt.
**Architekt:** Claude (Cowork) · **Stand:** Juni 2026
**Anlass:** Befund aus der Lokal-Provider-Evaluation — bedeutungsverändernde ASR-Fehler („nicht" → „nett") lassen sich durch *Text*-Korrektur prinzipiell nicht beheben, weil die Information nur im Audio steckt. Der echte Hebel liegt in der Transkriptions-Stufe.

---

## 1. Was wegfällt (und warum)

**Audio-natives lokales LLM (Gemma 4 E4B via llama.cpp) — vorerst geparkt.** Recherche Juni 2026:

- Die llama.cpp-Server-**Audio-Schnittstelle ist derzeit defekt/experimentell**: Audio-Input/STT wirft 500-Fehler bzw. liefert korrupten Output; der `input_audio`-Pfad ist im Server noch nicht fertig implementiert (offenes TODO).
- **Qualität wäre selbst dann unterlegen:** Whisper-Large ≈ 4,4 % WER vs. Gemma 3n ≈ 13 % WER. Gemmas Audio-Stärke ist *Reasoning über Sprache*, nicht akkurate Transkription.

→ Kein Aufwand hier, bis llama.cpp-Audio stabil ist. Bei Bedarf später erneut prüfen.

---

## 2. Die zwei verfügbaren Hebel (kein neuer Code)

Beide laufen über die **bestehende** Provider-Abstraktion — nur andere Parameter:

| Weg | Aufruf | Genauigkeit | Kosten | Datenschutz/Offline |
|---|---|---|---|---|
| **Cloud gpt-4o-transcribe** | `--provider openai --model gpt-4o-transcribe` | höchste | $0.006/Min | nein |
| **Lokal faster-whisper large-v3** | `--provider local --model large-v3` | beste Offline-Klasse (~Whisper-Large) | $0 | ja |

Referenzpunkte aus dem bisherigen Test: Cloud `gpt-4o-mini-transcribe` (Default) und lokal `small`/`medium` lagen bei den Bedeutungsfehlern darunter.

---

## 3. Evaluationsmethodik

Ziel: belastbar entscheiden, welcher Weg die *bedeutungskritischen* Fehler bei deinem realen Diktat löst — nicht anekdotisch an einem Clip.

1. **Testset:** 4–6 echte Diktat-Clips (je ~30–60 s), bewusst gemischt:
   - 1–2 sauber/ruhig,
   - 1–2 verrauscht (realer Alltag),
   - 1–2 mit Fachbegriffen/Eigennamen.
   Fest ablegen (z. B. `eval/clips/`), damit alle Modelle exakt dieselben Clips bekommen.
2. **Referenz (Ground Truth):** Zu jedem Clip einmal von Hand das *korrekte* Transkript schreiben. Das ist der Maßstab.
3. **Läufe:** Jeden Clip durch die fünf Kandidaten schicken — `gpt-4o-mini-transcribe`, `gpt-4o-transcribe`, lokal `small`, `medium`, `large-v3` — mit identischem Kontext-Prompt und `--output-dir` je Kandidat (Skip-Logik vermeiden).
4. **Bewertung:** Pro Lauf zählen:
   - **bedeutungskritische Fehler** (Sinnverkehrung, falsche Eigennamen) — das ist die Leitmetrik,
   - kosmetische Fehler (Zeichensetzung, Groß-/Klein) — nachrangig,
   - **Laufzeit** (lokal relevant) und **Kosten** (Cloud).
   Optional WER gegen die Referenz, wenn gewünscht; für die Entscheidung reicht meist das Zählen der Sinnfehler.
5. **Protokoll:** Eine kleine Tabelle (Clip × Modell → Sinnfehler / Zeit / Kosten). Kurt kann dafür ein schlankes Vergleichsskript bauen (alle Kandidaten über einen Clip, Ausgaben nebeneinander), ist aber kein Muss.

---

## 4. Entscheidungslogik

- **Wenn `gpt-4o-transcribe` die Sinnfehler zuverlässig löst** → Empfehlung: für *wichtige* Diktate Cloud `gpt-4o-transcribe`; `gpt-4o-mini-transcribe` bleibt günstiger Default für Alltag. In GUI als Qualitätsstufe anbieten.
- **Wenn `large-v3` nah an die Cloud herankommt** → als beste *Offline*-Option dokumentieren (Datenschutz/Volumen ohne Kosten), mit Hinweis auf Rechenlast/Tempo.
- **Wenn beide die Sinnfehler nicht lösen** → die Fehler sind audio-/aufnahmeseitig (Rauschen, Mikrofon). Dann ist der wirksamste Hebel die *Aufnahmequalität* (besseres Mikro, weniger Nebengeräusch) plus Kontext-Prompt — nicht ein anderes Modell.

---

## 5. Einordnung zum Text-Korrekturschritt (§7)

§7 (LLM-Korrektur) wird parallel mit **Chunking (Schritt 4)** fertiggestellt und ausgeliefert — als bescheidenes Optional-Feature für kosmetische Glättung. Es bleibt komplementär: ASR-Wahl adressiert die *Sinnfehler*, die Textkorrektur die *Oberfläche*. Beide Hebel greifen an verschiedenen Stellen, daher kein Entweder-oder.

---

## 6. Aufwand & Risiko

- **Niedrig.** Kein neuer Code nötig (beide Wege existieren). Hauptarbeit ist Messen + Urteilen.
- Einziger realer Aufwand: `large-v3` lädt beim ersten Lauf große Gewichte (~3 GB) und ist auf CPU deutlich langsamer als `small` — für die Evaluation einmalig vertretbar.
- Kosten der Cloud-Läufe sind vernachlässigbar (wenige Minuten Audio × $0.006).
