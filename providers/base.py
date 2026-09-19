from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TranscriptSegment:
    """Ein zeitlich begrenzter, unbearbeiteter ASR-Textabschnitt."""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TranscriptionResult:
    """Provider-Ergebnis mit Fließtext und optionalen Zeitsegmenten."""

    text: str
    segments: tuple[TranscriptSegment, ...] = ()


class TranscriptionProvider(ABC):
    """Abstrakte Transkriptions-Engine.

    Kapselt das eigentliche Speech-to-Text, sodass die Engine (OpenAI-Cloud,
    lokales faster-whisper, ...) ausgetauscht werden kann, ohne den restlichen
    Workflow (Splitting, Ersetzungen, Datei-I/O) in transcribe.py anzufassen.
    """

    name: str

    @abstractmethod
    def transcribe(self, audio_path: Path, language: str,
                   prompt: str | None = None,
                   hotwords: str | None = None) -> str:
        """Gibt den reinen Transkriptionstext zurück."""

    def transcribe_with_segments(
        self,
        audio_path: Path,
        language: str,
        prompt: str | None = None,
        hotwords: str | None = None,
    ) -> TranscriptionResult:
        """Gibt Text und, soweit verfügbar, Zeitsegmente zurück.

        Provider ohne Segmentunterstützung bleiben über den leeren
        Segment-Tupel abwärtskompatibel.
        """
        return TranscriptionResult(
            text=self.transcribe(audio_path, language, prompt, hotwords)
        )

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
