# Bugfix-Spec: Dauerbewusstes Splitting für gpt-4o-Transcribe

**Rolle:** Umsetzung durch Kurt (Claude Code). **Architekt:** Claude (Cowork) · **Stand:** Juni 2026
**Priorität:** hoch — betrifft den fast täglichen Realeinsatz (lange Diktate), nicht nur die Evaluation.

---

## 1. Problem

`gpt-4o-transcribe` und `gpt-4o-mini-transcribe` akzeptieren **max. ~1400–1500 s Audio** (~23–25 Min) pro Anfrage plus ein Token-Limit. `whisper-1` hat **nur** das 25-MB-Dateilimit, keine Dauergrenze.

Der Splitter in `transcribe.py` (`split_audio_file`) teilt **nach Dateigröße** (25 MB). Bei den verwendeten Bitraten entspricht eine 25-MB-Datei aber ~35–50 Min Audio — weit über 1400 s. Folge: size-gesplittete Teile sind für die gpt-4o-Modelle in der Dauer weiterhin zu lang → `400 input_too_large` bzw. `audio duration … longer than 1400 seconds`.

**Zweiter Defekt:** Der Split mit `-c copy` liefert Teildateien mit **falscher Dauer-Metadata** (beobachtet: Teil 01 meldete die volle Originaldauer 2102 s). Ursache vermutlich der mitkopierte VBR-Header (Xing/Info). Der Sollwert je Teil muss auch real in den Metadaten stehen.

---

## 2. Fix

### 2.1 Provider deklariert zwei Limits

`TranscriptionProvider` um `max_duration_seconds` erweitern (zusätzlich zu `max_file_size_mb`; `None` = kein Limit):

| Provider/Modell | max_file_size_mb | max_duration_seconds |
|---|---|---|
| OpenAI `whisper-1` | 25 | `None` |
| OpenAI `gpt-4o-transcribe` / `gpt-4o-mini-transcribe` | 25 | **1200** (Sicherheitspuffer unter 1400) |
| Local (faster-whisper) | `None` | `None` |

Der OpenAIProvider muss das Dauerlimit **modellabhängig** setzen (whisper-1 vs. gpt-4o-*).

### 2.2 Splitting an der bindenden Grenze

In `split_audio_file` die Teilezahl aus **beiden** Grenzen bestimmen:

```python
parts_by_size = ceil(file_size_mb / max_size_mb) if max_size_mb else 1
parts_by_dur  = ceil(total_duration / max_duration) if max_duration else 1
num_parts = max(parts_by_size, parts_by_dur, 1)
```

`transcribe.py` muss dem Splitter beide Limits des aktiven Providers übergeben (heute wird nur die Größe betrachtet). Splitting weiterhin nur, wenn überhaupt eine Grenze überschritten ist; lokaler Provider (beide `None`) → nie splitten (wie bisher).

### 2.3 ffmpeg-Split mit korrekter Dauer

Damit jede Teildatei ihre **echte** Dauer meldet (Behebung des 2102-s-Artefakts):

- `-reset_timestamps 1` ergänzen, **und/oder** beim Split neu kodieren statt `-c copy` (z. B. `libmp3lame`, analog `join_audio.py`).
- Empfehlung: Re-Encode pro Teil ist am robustesten gegen VBR-Header-Probleme. Falls `-c copy` aus Tempo-Gründen bevorzugt wird, zwingend mit korrekt gesetzten Timestamps und anschließendem ffprobe-Check der Teildauer.

---

## 3. Tests

- [ ] 35-Min-Datei, `--model gpt-4o-mini-transcribe`: wird in mehrere Teile < 1200 s gesplittet, **alle** Teile erfolgreich, Ergebnis zusammengefügt.
- [ ] Gleiche Datei, `--model gpt-4o-transcribe`: ebenfalls erfolgreich.
- [ ] ffprobe auf jede Teildatei: gemeldete Dauer entspricht real ~Sollwert (nicht volle Originaldauer).
- [ ] `--model whisper-1`: unverändert (nur Größen-Split, kein Dauer-Split).
- [ ] `--provider local`: unverändert, kein Split.
- [ ] Datei knapp unter/über Dauergrenze: korrekte Teilezahl (Randfall ceil).

---

## 4. Interim für den PO (bis der Fix steht)

- Lange Aufnahmen (> ~20 Min) über Cloud: `--model whisper-1` (kein Dauerlimit) **oder** lokal (`--provider local`).
- Für den gpt-4o-Qualitätsvergleich kurze Clips (≤ 20 Min, ideal die 30–60-s-Eval-Clips) — umgeht das Limit vollständig.

---

## 5. Hinweis zum Default-Modell

Der aktuelle Default `gpt-4o-mini-transcribe` ist für *kurze* Aufnahmen ideal (Kosten/Qualität), bei *langen* aber bis zu diesem Fix unbrauchbar. Nach dem Fix ist der Default unproblematisch. Sollte der Fix sich verzögern, wäre als Übergang ein dauerabhängiger Auto-Fallback (lange Datei + gpt-4o-Modell → Warnung/Hinweis auf whisper-1) denkbar — optional, nicht zwingend.
