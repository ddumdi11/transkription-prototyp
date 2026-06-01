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


class OpenAIProvider(TranscriptionProvider):
    """Transkription über die OpenAI-API (client.audio.transcriptions.create)."""

    name = "openai"

    def __init__(self, model: str = DEFAULT_MODEL):
        # .env laden (für OPENAI_API_KEY)
        load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY ist nicht gesetzt. "
                "Bitte in der .env-Datei oder als Umgebungsvariable hinterlegen."
            )

        self.model = model
        self._client = OpenAI(api_key=api_key)

    @property
    def max_file_size_mb(self) -> float | None:
        return MAX_FILE_SIZE_MB

    def transcribe(self, audio_path: Path, language: str,
                   prompt: str | None = None) -> str:
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
