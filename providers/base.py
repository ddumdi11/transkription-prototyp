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

    @property
    @abstractmethod
    def max_duration_seconds(self) -> float | None:
        """Dauergrenze pro Anfrage in Sekunden; None = kein Limit.

        Die gpt-4o-Transcribe-Modelle akzeptieren nur ~1400 s pro Anfrage
        (whisper-1 dagegen nur das Größenlimit). Wird zusätzlich zu
        max_file_size_mb fürs Splitting herangezogen."""
