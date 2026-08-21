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
                 compute_type: str = "auto", vad_filter: bool = True,
                 condition_on_previous_text: bool = True,
                 repetition_penalty: float = 1.0,
                 compression_ratio_threshold: float = 2.4,
                 temperature: float | list[float] | tuple[float, ...] = (
                     0.0, 0.2, 0.4, 0.6, 0.8, 1.0)):
        if WhisperModel is None:
            raise RuntimeError(
                "Lokale Transkription benötigt faster-whisper: "
                "pip install -r requirements-local.txt"
            ) from _IMPORT_ERROR

        # model_size=None -> Provider-eigener Default (siehe Plan §3.4a).
        self.model_size = model_size or DEFAULT_MODEL_SIZE

        # VAD (Voice Activity Detection) filtert Stille/Rauschen vor der
        # Transkription heraus. Reduziert Whisper-Wiederholungsartefakte auf
        # stummen/verrauschten Passagen. Default an, abschaltbar.
        self.vad_filter = vad_filter

        # Decoder-Parameter gegen Loops (Decoder-Wiederholungsschleifen).
        # Defaults = faster-whisper-Defaults, d. h. das bisherige Verhalten
        # bleibt unveraendert, bis der Aufrufer bewusst abweicht.
        #
        # - condition_on_previous_text: konditioniert die naechste Passage auf
        #   den bisher erzeugten Text. Whispers Default True ist die
        #   Hauptursache fuer Loops (Modell konditioniert auf eigenen Output);
        #   False durchbricht die Schleife, kann aber Kontext/Kohaerenz kosten.
        # - repetition_penalty: >1.0 bestraft Token-Wiederholung sanft.
        # - compression_ratio_threshold: erkennt entartete (stark komprimier-
        #   bare) Ausgaben und erzwingt einen Neu-Dekodier-Versuch.
        # - temperature: Fallback-Temperaturen bei Ausfall der Qualitaets-
        #   heuristiken; skalar oder Liste.
        #
        # BEWUSST NICHT hier: no_repeat_ngram_size. Der Sprecher wiederholt
        # sich absichtlich; ein harter n-Gramm-Block wuerde echte Inhalte
        # zerstoeren.
        self.condition_on_previous_text = condition_on_previous_text
        self.repetition_penalty = repetition_penalty
        self.compression_ratio_threshold = compression_ratio_threshold
        self.temperature = temperature

        # device="auto": CUDA falls verfügbar, sonst CPU.
        # Beim ersten Lauf werden die Modellgewichte heruntergeladen.
        print(
            f"[LOKAL] Lade Whisper-Modell '{self.model_size}' "
            f"(beim ersten Mal werden Gewichte heruntergeladen, das kann dauern)..."
        )
        self._model = WhisperModel(
            self.model_size, device=device, compute_type=compute_type
        )

    def runtime_description(self) -> str | None:
        """Tatsächlich verwendetes Device + compute_type (für Benchmarks).

        device/compute_type stehen i. d. R. auf "auto"; hier steht, was
        CTranslate2 daraus tatsächlich gewählt hat (z. B. cpu / int8_float32).
        Direkt aus der Modellinstanz gelesen, nicht geraten — None, wenn nicht
        zuverlässig auslesbar (dann lieber nichts als etwas Falsches).
        """
        try:
            ct2 = self._model.model
            return f"device={ct2.device}, compute_type={ct2.compute_type}"
        except Exception:
            return None

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
            vad_filter=self.vad_filter,
            condition_on_previous_text=self.condition_on_previous_text,
            repetition_penalty=self.repetition_penalty,
            compression_ratio_threshold=self.compression_ratio_threshold,
            temperature=self.temperature,
        )
        return " ".join(seg.text.strip() for seg in segments).strip()
