from pathlib import Path

from .base import TranscriptionProvider

# faster-whisper ist eine optionale, schwergewichtige Abhängigkeit
# (CTranslate2, lädt Modellgewichte). Reine Cloud-Nutzer sollen nichts
# extra installieren müssen -> Import defensiv behandeln.
try:
    from faster_whisper import WhisperModel
except ImportError as e:  # pragma: no cover - hängt von Installation ab
    WhisperModel = None
    _IMPORT_ERROR = e
else:
    _IMPORT_ERROR = None

# Default-Whisper-Größe für lokale Transkription (guter Kompromiss aus
# Tempo und Qualität für deutsches Diktat auf CPU).
DEFAULT_MODEL_SIZE = "small"


class LocalWhisperProvider(TranscriptionProvider):
    """Lokale Transkription mit faster-whisper (Whisper, CPU/GPU, kostenlos)."""

    name = "local"

    def __init__(self, model_size: str | None = None, device: str = "auto",
                 compute_type: str = "auto"):
        if WhisperModel is None:
            raise RuntimeError(
                "Lokale Transkription benötigt faster-whisper: "
                "pip install -r requirements-local.txt"
            ) from _IMPORT_ERROR

        # model_size=None -> Provider-eigener Default (siehe Plan §3.4a).
        self.model_size = model_size or DEFAULT_MODEL_SIZE

        # device="auto": CUDA falls verfügbar, sonst CPU.
        # Beim ersten Lauf werden die Modellgewichte heruntergeladen.
        print(
            f"[LOKAL] Lade Whisper-Modell '{self.model_size}' "
            f"(beim ersten Mal werden Gewichte heruntergeladen, das kann dauern)..."
        )
        self._model = WhisperModel(
            self.model_size, device=device, compute_type=compute_type
        )

    @property
    def max_file_size_mb(self) -> float | None:
        return None  # kein 25-MB-Limit -> Splitting für lokal nicht nötig

    @property
    def max_duration_seconds(self) -> float | None:
        return None  # faster-whisper verarbeitet beliebig lange Dateien am Stück

    def transcribe(self, audio_path: Path, language: str,
                   prompt: str | None = None) -> str:
        segments, _ = self._model.transcribe(
            str(audio_path),
            language=language,
            initial_prompt=prompt,  # Pendant zum OpenAI-prompt
        )
        return " ".join(seg.text.strip() for seg in segments).strip()
