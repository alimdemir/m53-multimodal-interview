import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from .audio_signal import signal_metrics


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AsrResult:
    text: str
    confidence: float | None
    language: str
    duration: float


class AsrEngine:
    """Lazy faster-whisper adapter; importing Django never loads model weights."""

    _model = None
    _lock = threading.Lock()
    _load_error: str | None = None
    _inference_lock = threading.BoundedSemaphore(2)

    @property
    def enabled(self) -> bool:
        return os.getenv("ASR_ENABLED", "0").lower() in {"1", "true", "yes", "on"}

    @property
    def model_path(self) -> Path:
        configured = os.getenv(
            "ASR_MODEL_PATH",
            str(settings.BASE_DIR / "models" / "faster-whisper-large-v3-turbo"),
        )
        return Path(configured)

    def status(self) -> dict:
        if not self.enabled:
            return {
                "state": "disabled",
                "label": "Transkripsiyon kapalı",
                "detail": "ASR_ENABLED=1 ile etkinleştirin.",
            }
        if not self.model_path.exists():
            return {
                "state": "missing",
                "label": "Whisper modeli bulunamadı",
                "detail": str(self.model_path),
            }
        if self._load_error:
            return {"state": "error", "label": "Whisper yüklenemedi", "detail": self._load_error}
        return {
            "state": "ready" if self._model is not None else "available",
            "label": "Whisper hazır",
            "detail": self.model_path.name,
        }

    def _load(self):
        if self._model is not None:
            return self._model
        if not self.enabled:
            raise RuntimeError("ASR servisi kapalı.")
        if not self.model_path.exists():
            raise RuntimeError(f"Whisper modeli bulunamadı: {self.model_path}")

        with self._lock:
            if self._model is not None:
                return self._model
            try:
                from faster_whisper import WhisperModel

                device = os.getenv("ASR_DEVICE", "cpu")
                default_compute = "float16" if device == "cuda" else "int8"
                self._model = WhisperModel(
                    str(self.model_path),
                    device=device,
                    compute_type=os.getenv("ASR_COMPUTE_TYPE", default_compute),
                    cpu_threads=int(os.getenv("ASR_CPU_THREADS", "4")),
                    num_workers=int(os.getenv("ASR_WORKERS", "1")),
                )
                self._load_error = None
            except Exception as exc:  # optional runtime dependency
                self._load_error = str(exc)
                logger.exception("Whisper modeli yüklenemedi")
                raise
        return self._model

    def transcribe_pcm(self, pcm_bytes: bytes, sample_rate: int = 16000) -> AsrResult:
        if not self._inference_lock.acquire(blocking=False):
            raise RuntimeError("Altyazı modeli meşgul.")
        try:
            return self._transcribe_pcm(pcm_bytes, sample_rate)
        finally:
            self._inference_lock.release()

    def _transcribe_pcm(self, pcm_bytes: bytes, sample_rate: int = 16000) -> AsrResult:
        import numpy as np

        if sample_rate != 16000 or len(pcm_bytes) % 2:
            raise ValueError("16 kHz mono PCM16 gerekli.")
        audio = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float32) / 32768.0
        if audio.size < sample_rate:
            return AsrResult(text="", confidence=None, language="tr", duration=audio.size / sample_rate)

        # This only rejects near-silence. Whisper's VAD still decides speech;
        # a short answer surrounded by pauses must reach that VAD.
        if signal_metrics(audio, sample_rate)["active_ms"] < 120:
            return AsrResult(text="", confidence=None, language="tr", duration=audio.size / sample_rate)

        model = self._load()
        segments, info = model.transcribe(
            audio,
            language="tr",
            beam_size=int(os.getenv("ASR_BEAM_SIZE", "1")),
            vad_filter=True,
            condition_on_previous_text=False,
            word_timestamps=False,
            initial_prompt=os.getenv(
                "ASR_INITIAL_PROMPT",
                "Türkçe teknik iş mülakatı. Yazılım, veri, ürün, proje ve ölçülebilir sonuçlar.",
            ),
        )
        realized = list(segments)
        text = " ".join(segment.text.strip() for segment in realized if segment.text.strip()).strip()
        if realized:
            # Uncalibrated decoder score, not a probability of transcription accuracy.
            confidence = float(np.mean([np.exp(min(0.0, segment.avg_logprob)) for segment in realized]))
        else:
            confidence = None
        return AsrResult(
            text=text,
            confidence=confidence,
            language=getattr(info, "language", "tr"),
            duration=audio.size / sample_rate,
        )


asr_engine = AsrEngine()
