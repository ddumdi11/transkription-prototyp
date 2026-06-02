from openai import OpenAI

from .base import Corrector

# Eng geführter Prompt (Plan §5): das LLM darf NUR offensichtliche
# Transkriptionsfehler korrigieren, nicht paraphrasieren/zusammenfassen.
SYSTEM_PROMPT = (
    "Du bist ein Korrektor für automatisch erstellte Transkripte deutschsprachiger\n"
    "Diktate. Korrigiere AUSSCHLIESSLICH offensichtliche Transkriptionsfehler:\n"
    "falsch erkannte Wörter, Zeichensetzung, Groß-/Kleinschreibung, Wortgrenzen.\n"
    "\n"
    "Strikte Regeln:\n"
    "- Bewahre Inhalt, Bedeutung, Sprache und Formulierung des Sprechers exakt.\n"
    "- Fasse NICHTS zusammen, ergänze NICHTS, lösche NICHTS, übersetze NICHT.\n"
    "- Erfinde keine Inhalte. Im Zweifel das Original beibehalten.\n"
    "- Keine Vorbemerkung, keine Erklärung, kein Markdown-Rahmen.\n"
    "- Gib ausschließlich den korrigierten Fließtext zurück."
)

# OpenAI-kompatible lokale Backends (Plan §2). Keine neue Abhängigkeit —
# die bereits installierte openai-Bibliothek wird wiederverwendet.
BACKEND_DEFAULTS = {
    "ollama": "http://localhost:11434/v1",
    "lmstudio": "http://localhost:1234/v1",
}


class LLMCorrector(Corrector):
    """Korrektur über ein lokales, OpenAI-kompatibles LLM (Ollama/LM Studio)."""

    def __init__(self, model: str, base_url: str, api_key: str = "local",
                 timeout: float = 120.0):
        # base_url/api_key zeigen auf den lokalen Server; der Key wird auf
        # localhost ignoriert, muss aber nicht-leer sein (SDK-Konstruktor).
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        self.model = model
        self.base_url = base_url

    def correct(self, text: str) -> str:
        resp = self._client.chat.completions.create(
            model=self.model,
            temperature=0,            # deterministisch, kein "Kreativ-Umschreiben"
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        )
        content = resp.choices[0].message.content
        if not content or not content.strip():
            raise RuntimeError("LLM lieferte eine leere Antwort.")
        return content.strip()


def build_corrector(backend: str | None, model: str | None,
                    base_url: str | None = None) -> LLMCorrector:
    """Erzeugt einen LLMCorrector und löst die Backend-Default-URL auf.

    base_url hat Vorrang vor der Backend-Default-URL (Plan §8: abweichende
    Ports/Hosts). Ein Modellname ist Pflicht.
    """
    if not model:
        raise ValueError(
            "Für die LLM-Korrektur ist ein Modellname nötig "
            "(--correct-model oder CORRECTION_MODEL in .env)."
        )

    url = base_url or BACKEND_DEFAULTS.get(backend or "ollama")
    if not url:
        erlaubt = ", ".join(BACKEND_DEFAULTS)
        raise ValueError(
            f"Unbekanntes Korrektur-Backend: {backend!r}. "
            f"Erlaubt: {erlaubt} (oder eigene --correct-base-url angeben)."
        )

    return LLMCorrector(model=model, base_url=url)
