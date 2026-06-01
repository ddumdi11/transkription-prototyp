from abc import ABC, abstractmethod
from pathlib import Path


class TranscriptionProvider(ABC):
    """Abstrakte Transkriptions-Engine.

    Kapselt das eigentliche Speech-to-Text, sodass die Engine (OpenAI-Cloud,
    lokales faster-whisper, ...) ausgetauscht werden kann, ohne den restlichen
    Workflow (Splitting, Ersetzungen, Datei-I/O) in transcribe.py anzufassen.
    """

    name: str

    @abstractmethod
    def transcribe(self, audio_path: Path, language: str,
                   prompt: str | None = None) -> str:
        """Gibt den reinen Transkriptionstext zurück."""

    @property
    @abstractmethod
    def max_file_size_mb(self) -> float | None:
        """Größenlimit pro Datei; None = kein Limit (z. B. lokal)."""
