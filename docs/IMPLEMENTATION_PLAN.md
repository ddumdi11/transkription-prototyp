# Implementierungs-Plan: Kostenoptimierung + lokale Transkription

**Rolle dieses Dokuments:** Spezifikation für die Umsetzung durch Kurt (Claude Code in VS Code).
**Architekt:** Claude (Cowork) · **PO:** Thorsten
**Stand:** Juni 2026

---

## 1. Ausgangslage & Ziele

Der Prototyp transkribiert Diktiergerät-Audio über die OpenAI-API (`client.audio.transcriptions.create`). Einziger Kostenpunkt ist dieser Transkriptions-Aufruf; die Wort-Ersetzungen laufen lokal und kosten nichts.

**Zwei Ziele:**

1. **Kosten senken** — sofort durch günstigeres Modell, langfristig durch eine kostenlose lokale Alternative.
2. **Provider-Wahl** — der Nutzer soll zwischen *OpenAI (Cloud)* und *Lokal (faster-whisper)* wählen können, in CLI **und** GUI.

**Wichtige Klarstellung:** Ollama und LM Studio sind LLM-Server und liefern *kein* sauberes Speech-to-Text. Die lokale Transkriptions-Engine ist **`faster-whisper`** (Whisper lokal, CPU/GPU). Ollama/LM Studio sind als *optionaler* späterer Korrekturschritt vorgesehen (siehe §7), nicht für die Transkription selbst.

---

## 2. Kostenanalyse (Quick Win, teils schon umgesetzt)

| Modell | Preis/Min | Preis/Stunde | Hinweis |
|---|---|---|---|
| `whisper-1` (alt) | $0.006 | $0.36 | bisheriger Default |
| `gpt-4o-transcribe` | $0.006 | $0.36 | beste Qualität |
| **`gpt-4o-mini-transcribe`** | **$0.003** | **$0.18** | **neuer Default — halber Preis, bessere Qualität** |
| **Lokal (faster-whisper)** | **$0** | **$0** | Ziel dieses Plans |

**Bereits umgesetzt (von Claude/Cowork):**

- `transcribe.py`: Default-Modell `whisper-1` → `gpt-4o-mini-transcribe`. `--model` bleibt frei wählbar, `whisper-1` weiter möglich.
- `.gitattributes`: normalisiert Zeilenenden (LF im Repo), beseitigt das CRLF↔LF-Diff-Rauschen.

Faustregel: €5 Guthaben ≈ 15 h Audio mit `whisper-1`, **≈ 30 h** mit `gpt-4o-mini-transcribe`, **unbegrenzt** lokal.

---

## 3. Zielarchitektur: Provider-Abstraktion

Heute ist die OpenAI-Logik fest in `transcribe.py` verdrahtet. Ziel ist eine schlanke Abstraktion, sodass die Transkriptions-Engine austauschbar wird, ohne den restlichen Workflow (Splitting, Ersetzungen, Datei-I/O) anzufassen.

```
transcribe.py            # Orchestrierung: sammeln, splitten, speichern, ersetzen
providers/
  __init__.py
  base.py                # TranscriptionProvider (ABC)
  openai_provider.py     # OpenAIProvider
  local_provider.py      # LocalWhisperProvider (faster-whisper)
  factory.py             # get_provider(name, **opts)
config.py                # Default-Provider/Modell, .env-Auswertung
```

### 3.1 Interface (`providers/base.py`)

```python
from abc import ABC, abstractmethod
from pathlib import Path

class TranscriptionProvider(ABC):
    name: str

    @abstractmethod
    def transcribe(self, audio_path: Path, language: str,
                   prompt: str | None = None) -> str:
        """Gibt den reinen Transkriptionstext zurück."""

    @property
    @abstractmethod
    def max_file_size_mb(self) -> float | None:
        """Größenlimit pro Datei; None = kein Limit (lokal)."""
```

### 3.2 OpenAIProvider (`providers/openai_provider.py`)

Kapselt die heutige Logik aus `transcribe_file()`:

- Konstruktor: `model` (Default `gpt-4o-mini-transcribe`), liest `OPENAI_API_KEY` aus `.env`.
- `max_file_size_mb` → `25` (API-Limit).
- `transcribe()` ruft `client.audio.transcriptions.create(...)` und gibt `result.text`.

### 3.3 LocalWhisperProvider (`providers/local_provider.py`)

```python
from faster_whisper import WhisperModel

class LocalWhisperProvider(TranscriptionProvider):
    name = "local"

    def __init__(self, model_size: str = "small", device: str = "auto",
                 compute_type: str = "auto"):
        # device="auto": CUDA falls verfügbar, sonst CPU
        self._model = WhisperModel(model_size, device=device,
                                   compute_type=compute_type)

    @property
    def max_file_size_mb(self):
        return None  # kein 25-MB-Limit → Splitting für lokal nicht nötig

    def transcribe(self, audio_path, language, prompt=None):
        segments, _ = self._model.transcribe(
            str(audio_path), language=language,
            initial_prompt=prompt,  # Pendant zum OpenAI-prompt
        )
        return " ".join(seg.text.strip() for seg in segments).strip()
```

**Modellgrößen** (`faster-whisper`): `tiny`/`base`/`small`/`medium`/`large-v3`. Für deutsches Diktat ist `small` oder `medium` ein guter Kompromiss aus Tempo und Qualität auf CPU; mit GPU geht `large-v3`. Default-Vorschlag: `small`, in der GUI wählbar.

### 3.4 Factory (`providers/factory.py`)

```python
def get_provider(provider: str, **opts) -> TranscriptionProvider:
    if provider == "openai":
        return OpenAIProvider(model=opts.get("model", "gpt-4o-mini-transcribe"))
    if provider == "local":
        return LocalWhisperProvider(model_size=opts.get("model_size", "small"))
    raise ValueError(f"Unbekannter Provider: {provider}")
```

### 3.4a WICHTIG: `--model`-Semantik (Stolperfalle)

`--model` bedeutet je nach Provider etwas anderes: bei OpenAI ein Modellname
(`gpt-4o-mini-transcribe`), bei lokal eine Whisper-Größe (`small`, `medium`, …).
Der aktuelle Default `gpt-4o-mini-transcribe` ist als Whisper-Größe **ungültig** —
würde man ihn an `--provider local` durchreichen, bricht faster-whisper ab.

**Lösung:** `--model` auf `default=None` setzen. Jeder Provider wählt seinen eigenen
Default, wenn `model` None ist:

- `OpenAIProvider`: None → `gpt-4o-mini-transcribe`
- `LocalWhisperProvider`: None → `small`

So bleibt ein einzelnes `--model`-Flag korrekt, ohne providerübergreifende Kollision.
Die Hilfetexte entsprechend anpassen.

### 3.5 Anpassung `transcribe.py`

- Neues CLI-Argument `--provider {openai,local}` (Default `openai`).
- `transcribe_file()` ruft statt des direkten OpenAI-Clients `provider.transcribe(...)`.
- **Splitting nur wenn nötig:** `if provider.max_file_size_mb and size > limit:` — beim lokalen Provider entfällt das 25-MB-Splitting komplett (großer Vorteil bei langen Aufnahmen).
- Ersetzungen, Datei-I/O, Skip-Logik, Markdown-Titel bleiben unverändert.

---

## 4. GUI-Änderungen (`gui.py`)

- Neues Dropdown **„Engine"**: `OpenAI (Cloud, kostenpflichtig)` / `Lokal (faster-whisper, kostenlos)`.
- Abhängiges Dropdown **„Modell"**:
  - OpenAI: `gpt-4o-mini-transcribe` (Default), `gpt-4o-transcribe`, `whisper-1`.
  - Lokal: `tiny`, `base`, `small` (Default), `medium`, `large-v3`.
- Auswahl als zusätzliche CLI-Argumente (`--provider`, `--model`) an den Subprozess durchreichen — passt zur bestehenden `_run_command`-Mechanik.
- Optional: kleine Kostenanzeige/Hinweis neben der Engine-Wahl.
- **Einstellungen merken** (kleines JSON neben der App), damit die Provider-Wahl nicht bei jedem Start zurückgesetzt wird.

---

## 5. Abhängigkeiten

`faster-whisper` ist eine schwergewichtige Abhängigkeit (CTranslate2, lädt Modellgewichte). Daher **optional** halten, damit reine Cloud-Nutzer nichts extra installieren müssen.

`requirements.txt` unverändert lassen, zusätzlich `requirements-local.txt`:

```
faster-whisper>=1.0.0
```

In `local_provider.py` den Import defensiv behandeln:

```python
try:
    from faster_whisper import WhisperModel
except ImportError as e:
    raise RuntimeError(
        "Lokale Transkription benötigt faster-whisper: "
        "pip install -r requirements-local.txt"
    ) from e
```

`ffmpeg` ist bereits Voraussetzung (für `join_audio.py`) und deckt auch faster-whisper ab.

---

## 6. Umsetzungsschritte für Kurt (geordnet)

1. **Provider-Gerüst anlegen:** `providers/` mit `base.py`, `factory.py`.
2. **OpenAI-Logik auslagern:** Bestehenden Code aus `transcribe.py` in `OpenAIProvider` verschieben; `transcribe.py` auf den Provider umstellen. Funktional muss alles wie bisher laufen (Regressionstest mit einer kleinen Audiodatei, Provider `openai`).
3. **CLI erweitern:** `--provider` einbauen, Splitting an `max_file_size_mb` koppeln.
4. **LocalWhisperProvider:** implementieren, `requirements-local.txt` anlegen, defensiver Import.
5. **GUI:** Engine-/Modell-Dropdowns + Durchreichen der Argumente + Einstellungen merken.
6. **Doku:** README um Abschnitt „Engine wählen: Cloud vs. Lokal" und Kostenvergleich ergänzen; CHECKPOINT fortschreiben.
7. **Tests:** §8 durchgehen.

Schritte 1–3 sind das risikoarme Refactoring (keine Verhaltensänderung) und sollten **zuerst committet** werden. Erst danach 4–6 als eigene Commits.

---

## 7. Zukunftsmusik: optionaler LLM-Korrekturschritt (Ollama/LM Studio)

Hier passen Ollama/LM Studio *richtig* rein — nicht für ASR, sondern für die **intelligente Nachkorrektur** statt der heutigen statischen Wort-Ersetzungen:

- Nach der Transkription Text an ein lokales LLM (Ollama/LM Studio, OpenAI-kompatible API auf `localhost`) geben mit Prompt „Korrigiere Erkennungsfehler, behalte Inhalt und Sprache bei".
- Als dritter Provider-Typ `PostProcessor` modellierbar, ebenfalls optional/abschaltbar.
- Vorteil: ersetzt das pflegeintensive `DEFAULT_REPLACEMENTS`-Dictionary durch kontextbewusste Korrektur — lokal und kostenlos.

Bewusst **nicht** Teil dieser Iteration; erst lokale Transkription stabilisieren.

---

## 8. Test-Checkliste

- [ ] `--provider openai` mit Default-Modell: Transkript identisch zu vorher (Regressionstest).
- [ ] `--provider openai --model whisper-1`: weiterhin funktionsfähig.
- [ ] Datei >25 MB mit OpenAI: Splitting greift wie bisher.
- [ ] `--provider local`: läuft ohne API-Key, kein Netzwerk.
- [ ] Datei >25 MB lokal: **kein** Splitting nötig, Transkript am Stück.
- [ ] Wort-Ersetzungen & `--no-replacements` wirken bei beiden Providern.
- [ ] GUI: Engine-Wechsel ändert Modell-Dropdown korrekt; Argumente kommen am Subprozess an.
- [ ] `faster-whisper` nicht installiert + Provider `local` → klare, freundliche Fehlermeldung.
- [ ] Sprache `de` und Kontext-Prompt wirken bei beiden Providern.

---

## 9. Plattform-Ausblick (Zukunftsmusik)

Die Provider-Abstraktion ist die Grundlage für die spätere Plattformunabhängigkeit: Eine spätere Desktop-/Mobile-Variante kann dieselben Provider hinter einer anderen UI nutzen. Lokale Transkription auf dem Smartphone ist anspruchsvoll (Rechenlast) — dort bleibt Cloud sinnvoll, am Desktop die kostenlose lokale Option. Das spricht für genau diese austauschbare Architektur.
