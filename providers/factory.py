from .base import TranscriptionProvider
from .openai_provider import DEFAULT_MODEL, OpenAIProvider


def get_provider(provider: str, **opts) -> TranscriptionProvider:
    """Erzeugt die gewünschte Transkriptions-Engine.

    opts:
      - model: Modellname für OpenAI (Default: gpt-4o-mini-transcribe).
    """
    if provider == "openai":
        return OpenAIProvider(model=opts.get("model") or DEFAULT_MODEL)
    raise ValueError(f"Unbekannter Provider: {provider}")
