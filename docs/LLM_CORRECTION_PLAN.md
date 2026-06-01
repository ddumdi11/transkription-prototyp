# Implementierungs-Plan: Optionaler LLM-Korrekturschritt (§7-Ausbau)

**Rolle:** Spezifikation für die Umsetzung durch Kurt (Claude Code).
**Architekt:** Claude (Cowork) · **PO:** Thorsten · **Stand:** Juni 2026
**Vorgänger:** `docs/IMPLEMENTATION_PLAN.md` (Provider-Abstraktion, abgeschlossen: Commits `6dfd947`, `66ee3e4`).

---

## 1. Ziel & Einordnung

Der Qualitätsvergleich beim Lokal-Provider hat gezeigt: ASR-Engines machen *kontextabhängige* Fehler (z. B. „ist nicht" → „ist nett", „Sprachnachricht" → „Sprachenerricht"), die das statische `DEFAULT_REPLACEMENTS`-Wörterbuch grundsätzlich nicht abfangen kann. Ein **lokales LLM** kann solche Fehler im Kontext erkennen und korrigieren.

**Ziel:** Ein *optionaler*, abschaltbarer Nachkorrektur-Schritt, der das fertige Transkript (egal von welchem Provider) durch ein lokal laufendes LLM (Ollama **oder** LM Studio) schickt. Er ergänzt die bestehenden Wort-Ersetzungen, ersetzt sie aber nicht zwingend.

**Nicht-Ziele:** Kein Zwang zu einem laufenden LLM-Server; keine neue schwergewichtige Abhängigkeit; keine inhaltliche Veränderung des Diktats (keine Zusammenfassung, keine Übersetzung).

---

## 2. Backend: OpenAI-kompatible lokale API

Ollama und LM Studio bieten beide eine **OpenAI-kompatible** Chat-Completions-API auf `localhost`. Damit wird die bereits installierte `openai`-Bibliothek wiederverwendet — nur `base_url` und `api_key` ändern sich. **Keine neue Abhängigkeit.**

| Backend | base_url | api_key | Hinweis |
|---|---|---|---|
| Ollama | `http://localhost:11434/v1` | beliebig (z. B. `"ollama"`) | Modell muss vorher `ollama pull <modell>` sein |
| LM Studio | `http://localhost:1234/v1` | beliebig (z. B. `"lm-studio"`) | Modell im Server-Tab laden; Port änderbar |

Beide ignorieren den API-Key auf localhost, verlangen aber einen nicht-leeren String (SDK-Konstruktor).

---

## 3. Architektur: Corrector-Abstraktion

Analog zur Provider-Abstraktion, aber als eigenständige, *optionale* Stufe nach der Transkription.

```
correction/
  __init__.py
  base.py            # Corrector (ABC) mit correct(text) -> str
  llm_corrector.py   # LLMCorrector (OpenAI-kompatibel, base_url/model)
```

### 3.1 Interface (`correction/base.py`)

```python
from abc import ABC, abstractmethod

class Corrector(ABC):
    @abstractmethod
    def correct(self, text: str) -> str:
        """Gibt den korrigierten Text zurück. Darf bei Fehlern eine
        Exception werfen — der Aufrufer fängt sie ab (siehe §6)."""
```

### 3.2 LLMCorrector (`correction/llm_corrector.py`)

```python
from openai import OpenAI

BACKEND_DEFAULTS = {
    "ollama":   "http://localhost:11434/v1",
    "lmstudio": "http://localhost:1234/v1",
}

class LLMCorrector(Corrector):
    def __init__(self, model: str, base_url: str, api_key: str = "local",
                 timeout: float = 120.0):
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        self._model = model

    def correct(self, text: str) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            temperature=0,            # deterministisch, kein "Kreativ-Umschreiben"
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        )
        return resp.choices[0].message.content.strip()
```

---

## 4. Pipeline-Einordnung (`transcribe.py`)

Reihenfolge pro Datei:

```
transcribe (Provider)  →  Wort-Ersetzungen (bestehend)  →  [optional] LLM-Korrektur  →  speichern
```

- Die billigen, deterministischen Ersetzungen laufen zuerst (instant, kostenlos).
- Die LLM-Korrektur ist **standardmäßig aus** und greift nur bei `--correct`.
- **Wichtig (§6):** Schlägt die LLM-Korrektur fehl, wird der *unkorrigierte* (bereits ersetzte) Text gespeichert — niemals geht ein Transkript verloren.

---

## 5. Der Korrektur-Prompt (kritisch)

Das LLM muss **eng geführt** werden, sonst paraphrasiert oder halluziniert es. Vorgabe für `SYSTEM_PROMPT`:

```
Du bist ein Korrektor für automatisch erstellte Transkripte deutschsprachiger
Diktate. Korrigiere AUSSCHLIESSLICH offensichtliche Transkriptionsfehler:
falsch erkannte Wörter, Zeichensetzung, Groß-/Kleinschreibung, Wortgrenzen.

Strikte Regeln:
- Bewahre Inhalt, Bedeutung, Sprache und Formulierung des Sprechers exakt.
- Fasse NICHTS zusammen, ergänze NICHTS, lösche NICHTS, übersetze NICHT.
- Erfinde keine Inhalte. Im Zweifel das Original beibehalten.
- Keine Vorbemerkung, keine Erklärung, kein Markdown-Rahmen.
- Gib ausschließlich den korrigierten Fließtext zurück.
```

- `temperature=0`.
- Modellwahl ist qualitätsentscheidend: ein fähiges Instruct-Modell (Richtwert ≥ 7–8B) deutlich besser als ein 3B-Modell, das selbst Fehler einbaut. In der Doku als Hinweis vermerken; keinen festen Default erzwingen (der Nutzer muss ein geladenes Modell angeben).

---

## 6. Robustheit: eine Korrektur darf nie ein Transkript verlieren

Das ist die wichtigste nichtfunktionale Anforderung — besonders, weil das Transkript bei Cloud bereits **bezahlt** wurde.

In `transcribe.py` die Korrektur defensiv kapseln:

```python
if corrector is not None:
    try:
        text = corrector.correct(text)
    except Exception as e:
        print(f"   [!] LLM-Korrektur fehlgeschlagen ({e}); "
              f"speichere unkorrigiertes Transkript.")
        # text bleibt unverändert -> wird trotzdem gespeichert
```

Abzudeckende Fehlerfälle: LLM-Server nicht erreichbar (Connection refused), Timeout, Modell nicht geladen/gefunden, leere Antwort. In allen Fällen: Warnung + unkorrigiertes Transkript speichern, Verarbeitung der restlichen Dateien fortsetzen.

**Zusätzliche Absicherung gegen „verschluckten" Inhalt:** Wenn die LLM-Antwort verdächtig kurz ist (z. B. < 50 % der Originallänge), als unsicher behandeln, Warnung ausgeben und das Original behalten. Schützt vor Modellen, die fälschlich zusammenfassen.

---

## 7. Lange Transkripte: Chunking

Lokale LLMs haben begrenzten Kontext; lange Diktate können das Fenster sprengen oder zu Qualitätsabfall am Ende führen.

- Wenn der Text eine konfigurierbare Schwelle überschreitet (Richtwert ~6 000 Zeichen), in Abschnitte an Absatz-/Satzgrenzen splitten, jeden Abschnitt einzeln korrigieren, dann zusammenfügen.
- Niemals mitten im Wort/Satz schneiden.
- Erste Iteration darf simpel sein (ganzer Text, falls kurz genug); Chunking als sauberer Pfad für lange Aufnahmen einbauen, da das der reale Anwendungsfall ist.

---

## 8. Konfiguration

### CLI (`transcribe.py`)
- `--correct` — LLM-Korrektur aktivieren (Default: aus).
- `--correct-backend {ollama,lmstudio}` — setzt die Default-`base_url` (siehe §2).
- `--correct-model NAME` — Modellname (Pflicht, wenn `--correct`; kein universeller Default).
- `--correct-base-url URL` — überschreibt die Backend-Default-URL (für abweichende Ports/Hosts).

### .env (einmal konfigurieren)
```
CORRECTION_BACKEND=ollama
CORRECTION_MODEL=<dein-geladenes-modell>
# optional:
CORRECTION_BASE_URL=http://localhost:11434/v1
```
CLI-Argumente haben Vorrang vor `.env`.

### GUI (`gui.py`)
- Checkbox **„LLM-Korrektur (lokal)"**.
- Wenn aktiv: Dropdown Backend (Ollama/LM Studio) + Eingabefeld Modellname.
- Auswahl als `--correct`/`--correct-backend`/`--correct-model` durchreichen.
- In `gui_settings.json` persistieren (wie die Engine-Einstellungen).

---

## 9. Abhängigkeiten

**Keine neuen.** `openai` ist bereits in `requirements.txt`. Ollama bzw. LM Studio sind externe Anwendungen, die der Nutzer separat installiert und startet — in der README als Voraussetzung dokumentieren, nicht als pip-Paket.

---

## 10. Umsetzungsschritte für Kurt (geordnet)

1. **Corrector-Gerüst:** `correction/base.py` + `correction/llm_corrector.py` mit `SYSTEM_PROMPT` aus §5.
2. **Factory/Erzeugung:** kleine Hilfsfunktion `build_corrector(backend, model, base_url)` (Backend → Default-URL auflösen).
3. **`transcribe.py` verdrahten:** CLI-Flags (§8), Corrector nach den Ersetzungen, defensiv gekapselt (§6). `.env`-Auswertung mit CLI-Vorrang.
4. **Chunking (§7):** für lange Transkripte.
5. **GUI (§8):** Checkbox + Backend/Modell + Persistenz.
6. **Doku:** README-Abschnitt „LLM-Korrektur (optional, lokal)" inkl. Voraussetzung Ollama/LM Studio und Modell-Empfehlung; CHECKPOINT fortschreiben.

Schritte 1–3 sind der Kern und sollten als **eigener Commit** vor GUI/Chunking-Feinheiten landen.

---

## 11. Test-Checkliste

- [ ] `--correct` aus (Default): Verhalten exakt wie bisher, kein LLM-Aufruf.
- [ ] `--correct` mit laufendem Ollama: korrigiert eine bekannte ASR-Fehlerstelle (z. B. „ist nett" → „ist nicht") **ohne** den Inhalt sonst zu verändern.
- [ ] Gleicher Lauf gegen LM Studio (`--correct-backend lmstudio`).
- [ ] **Server aus / nicht erreichbar:** Warnung, unkorrigiertes Transkript wird gespeichert, kein Absturz, nächste Datei läuft weiter.
- [ ] Falscher/nicht geladener Modellname: saubere Warnung, Original bleibt erhalten.
- [ ] Langer Text (> Schwelle): Chunking greift, Ausgabe vollständig, keine Schnitte mitten im Satz.
- [ ] Längen-Schutz: künstlich verkürzte LLM-Antwort wird als unsicher erkannt, Original behalten.
- [ ] Zusammenspiel mit `--no-replacements` und beiden Transkriptions-Providern.
- [ ] Inhaltstreue-Stichprobe: LLM fügt nichts hinzu/entfernt nichts (manuelle Sichtprüfung an einem realen Diktat).

---

## 12. Risiken & Caveats

- **Halluzination/Paraphrase:** Selbst mit striktem Prompt kann ein schwaches Modell umformulieren. Gegenmittel: `temperature=0`, enger Prompt, Längen-Schutz (§6), fähiges Modell.
- **Modellwahl dominiert die Qualität:** Ergebnis steht und fällt mit dem lokalen Modell — vor GUI-Default an realem Diktat messen.
- **Latenz:** Zusätzlicher LLM-Durchlauf kostet Zeit (lokal, CPU-abhängig). Akzeptabel, da optional und kostenlos.
- **Abhängigkeit vom laufenden Server:** Bewusst optional gehalten; Default bleibt aus.
- **Datenschutz-Konsistenz:** Lokales LLM passt zum Datenschutz-Argument des Lokal-Providers — Text verlässt den Rechner nicht. (Würde man ein Cloud-LLM für die Korrektur erlauben, ginge dieser Vorteil verloren — daher hier bewusst auf lokale Backends beschränkt.)
