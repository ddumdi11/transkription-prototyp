from .base import TranscriptionProvider
from .local_provider import LocalWhisperProvider
from .openai_provider import OpenAIProvider


def get_provider(provider: str, **opts) -> TranscriptionProvider:
    """Erzeugt die gewünschte Transkriptions-Engine.

    opts:
      - model: bei OpenAI ein Modellname (z. B. gpt-4o-mini-transcribe),
        bei lokal eine Whisper-Größe (z. B. small). None = Provider-Default
        (siehe Plan §3.4a).
    """
    model = opts.get("model")
    if provider == "openai":
        return OpenAIProvider(model=model)
    if provider == "local":
        return LocalWhisperProvider(model_size=model)
    raise ValueError(f"Unbekannter Provider: {provider}")
