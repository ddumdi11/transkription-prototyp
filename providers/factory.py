from .base import TranscriptionProvider
from .local_provider import LocalWhisperProvider
from .openai_provider import OpenAIProvider


# Optionale Engine-/Decoder-Parameter, die der lokale Provider akzeptiert und
# die die Factory bei Bedarf durchreicht (nur was gesetzt ist). Bewusst kein
# CLI-Flag: das sind programmatische Hebel (u. a. gegen Decoder-Loops), kein
# User-Feature — Defaults bleiben unveraendert (siehe
# docs/LOOP_INVESTIGATION_2026-07.md).
_LOCAL_OPT_KEYS = (
    "device", "compute_type", "vad_filter", "condition_on_previous_text",
    "repetition_penalty", "compression_ratio_threshold", "temperature",
)


def get_provider(provider: str, **opts) -> TranscriptionProvider:
    """Erzeugt die gewünschte Transkriptions-Engine.

    opts:
      - model: bei OpenAI ein Modellname (z. B. gpt-4o-mini-transcribe),
        bei lokal eine Whisper-Größe (z. B. small). None = Provider-Default
        (siehe Plan §3.4a).
      - lokal zusätzlich (optional): device, compute_type, vad_filter,
        condition_on_previous_text, repetition_penalty,
        compression_ratio_threshold, temperature. Nur gesetzte Werte werden
        durchgereicht; sonst greifen die Provider-Defaults.
    """
    model = opts.get("model")
    if provider == "openai":
        return OpenAIProvider(model=model)
    if provider == "local":
        local_opts = {k: opts[k] for k in _LOCAL_OPT_KEYS if k in opts}
        return LocalWhisperProvider(model_size=model, **local_opts)
    raise ValueError(f"Unbekannter Provider: {provider}")
