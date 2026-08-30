import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from .base import TranscriptionProvider

# Standard-Modell: gpt-4o-mini-transcribe ($0.003/Min, ~halber Preis von
# whisper-1 bei besserer Qualität).
DEFAULT_MODEL = "gpt-4o-mini-transcribe"

# Whisper API Limit: 25 MB
MAX_FILE_SIZE_MB = 25

# Dauergrenze der gpt-4o-Transcribe-Modelle: ~1400 s pro Anfrage. Mit
# Sicherheitspuffer auf 1200 s gesetzt. whisper-1 hat keine Dauergrenze.
GPT4O_MAX_DURATION_SECONDS = 1200


class OpenAIProvider(TranscriptionProvider):
    """Transkription über die OpenAI-API (client.audio.transcriptions.create)."""

    name = "openai"

    def __init__(self, model: str | None = None):
        # .env laden (für OPENAI_API_KEY)
        load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY ist nicht gesetzt. "
                "Bitte in der .env-Datei oder als Umgebungsvariable hinterlegen."
            )

        # model=None -> Provider-eigener Default (siehe Plan §3.4a).
        self.model = model or DEFAULT_MODEL
        self._client = OpenAI(api_key=api_key)

    @property
    def max_file_size_mb(self) -> float | None:
        return MAX_FILE_SIZE_MB

    @property
    def max_duration_seconds(self) -> float | None:
        # whisper-1 kennt nur das Größenlimit; die gpt-4o-Modelle haben
        # zusätzlich eine Dauergrenze pro Anfrage.
        if self.model == "whisper-1":
            return None
        return GPT4O_MAX_DURATION_SECONDS

    def transcribe(self, audio_path: Path, language: str,
                   prompt: str | None = None,
                   hotwords: str | None = None) -> str:
        # hotwords ist eine faster-whisper-Funktion. Beim Cloud-Provider bleibt
        # der Kontext-Prompt der vorgesehene Weg für Vokabularhinweise.
        with audio_path.open("rb") as f:
            kwargs = {
                "model": self.model,
                "file": f,
                "language": language,
            }
            if prompt:
                kwargs["prompt"] = prompt

            result = self._client.audio.transcriptions.create(**kwargs)

        return result.text
