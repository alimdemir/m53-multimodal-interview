"""Experimental, ephemeral candidate signals. Never feeds decisions or questions."""
import io
import logging
import math
import os
import time

from .hf_runtime import device_name, emotion_slot, model_available, model_path
from .audio_signal import signal_metrics

logger = logging.getLogger(__name__)
LABELS = {
    "angry": "Öfke ifadesi", "anger": "Öfke ifadesi", "calm": "Sakin ton",
    "happy": "Neşe ifadesi", "joy": "Neşe ifadesi", "sad": "Üzüntü ifadesi",
    "sadness": "Üzüntü ifadesi", "neutral": "Nötr ifade", "fear": "Korku ifadesi",
    "disgust": "Tiksinme ifadesi", "surprise": "Şaşkınlık ifadesi",
}


class EmotionEngine:
    def __init__(self):
        self.models = {}
        self.errors = {}
        self.face_detector = None

    @property
    def enabled(self):
        return os.getenv("EMOTION_ENABLED", "0").lower() in {"1", "true", "yes", "on"}

    def status(self):
        return {
            modality: {
                "state": "disabled" if not self.enabled else (
                    "error" if modality in self.errors else (
                        "ready" if modality in self.models else (
                            "available" if model_available(f"emotion-{modality}") else "missing"))),
                "label": {"text": "Türkçe BERT", "audio": "Türkçe HuBERT", "video": "Yüz ifadesi ViT"}[modality],
            }
            for modality in ("text", "audio", "video")
        }

    def _load(self, modality):
        if modality in self.models:
            return self.models[modality]
        if not model_available(f"emotion-{modality}"):
            raise FileNotFoundError("Model indirilmemiş.")
        import torch
        from transformers import (
            AutoTokenizer, AutoImageProcessor, AutoModelForImageClassification,
            AutoModelForSequenceClassification, Wav2Vec2FeatureExtractor,
        )

        torch.set_num_threads(int(os.getenv("HF_CPU_THREADS", "4")))
        path = str(model_path(f"emotion-{modality}"))
        options = {"local_files_only": True, "trust_remote_code": False}
        if modality == "text":
            processor = AutoTokenizer.from_pretrained(path, **options)
            cls = AutoModelForSequenceClassification
        elif modality == "video":
            processor = AutoImageProcessor.from_pretrained(path, use_fast=False, **options)
            cls = AutoModelForImageClassification
        else:
            from .turkish_hubert import TurkishHubertClassifier
            processor = Wav2Vec2FeatureExtractor.from_pretrained(path, **options)
            cls = TurkishHubertClassifier
        model, info = cls.from_pretrained(path, use_safetensors=True, output_loading_info=True, **options)
        # A random, newly initialized classifier must never masquerade as pretrained.
        if info["missing_keys"] or info.get("mismatched_keys"):
            raise ValueError("Checkpoint ile mimari eşleşmiyor; tahmin kapatıldı.")
        if any(str(label).lower() not in LABELS for label in model.config.id2label.values()):
            raise ValueError("Model duygu etiketleri doğrulanamadı.")
        model.to(device_name()).eval()
        self.models[modality] = processor, model
        self.errors.pop(modality, None)
        return processor, model

    @staticmethod
    def unavailable(modality, reason, state="no_data"):
        return {"modality": modality, "state": state, "detail": reason, "scores": [], "calibrated": False}

    def analyze(self, modality, value):
        if not self.enabled:
            return self.unavailable(modality, "Koçluk analizi sunucuda kapalı.", "disabled")
        if modality not in {"text", "audio", "video"}:
            raise ValueError("Geçersiz modalite")
        if not emotion_slot.acquire(timeout=8):
            return self.unavailable(modality, "Model meşgul; sonraki pencere beklenecek.", "busy")
        started = time.monotonic()
        try:
            import numpy as np
            import torch

            if modality == "text":
                if not isinstance(value, str) or len(value.strip()) < 8:
                    return self.unavailable(modality, "Daha uzun bir cümle bekleniyor.")
                value = value[:2000]
            elif modality == "audio":
                value = np.frombuffer(value, dtype="<i2").astype(np.float32) / 32768
                if not 16000 <= value.size <= 16000 * 6:
                    return self.unavailable(modality, "1–6 saniye, 16 kHz mono ses gerekli.")
                signal = signal_metrics(value)
                if signal["active_ms"] < 240:
                    return {**self.unavailable(modality, "Ses ulaştı; yeterli seviyede en az 240 ms ses bekleniyor."), "signal": signal}
                if signal["clipped_fraction"] > .10:
                    return {**self.unavailable(modality, "Ses kırpılıyor; mikrofon seviyesini azaltın."), "signal": signal}
            else:
                value, reason = self._face(value)
                if value is None:
                    return self.unavailable(modality, reason)
            processor, model = self._load(modality)
            if modality == "text":
                inputs = processor(value, truncation=True, max_length=128, return_tensors="pt")
            elif modality == "audio":
                inputs = processor(value, sampling_rate=16000, return_tensors="pt")
            else:
                inputs = processor(images=value, return_tensors="pt")
            inputs = {key: tensor.to(model.device) for key, tensor in inputs.items()}
            with torch.inference_mode():
                probabilities = model(**inputs).logits.softmax(-1)[0].float().cpu().tolist()
            if not all(math.isfinite(p) for p in probabilities):
                raise ValueError("Geçersiz model çıktısı")
            scores = sorted([
                {"label": str(model.config.id2label[i]).lower(),
                 "label_tr": LABELS[str(model.config.id2label[i]).lower()], "score": round(p, 5)}
                for i, p in enumerate(probabilities)
            ], key=lambda item: item["score"], reverse=True)
            entropy = -sum(p * math.log(max(p, 1e-12)) for p in probabilities) / math.log(len(probabilities))
            return {
                "modality": modality, "state": "uncertain" if scores[0]["score"] < .55 or entropy > .8 else "ok",
                "scores": scores, "entropy": round(entropy, 4), "calibrated": False,
                "detail": "Model skoru; doğruluk olasılığı veya kişinin gerçek duygusu değildir.",
                "latency_ms": round((time.monotonic() - started) * 1000),
                **({"signal": signal} if modality == "audio" else {}),
            }
        except Exception as exc:
            self.errors[modality] = type(exc).__name__
            logger.warning("Duygu modeli %s çalışmadı: %s", modality, type(exc).__name__)
            return self.unavailable(modality, "Model çalıştırılamadı. Model durumu ve kurulumunu kontrol edin.", "error")
        finally:
            emotion_slot.release()

    def _face(self, value):
        import cv2
        import numpy as np
        from PIL import Image, UnidentifiedImageError

        if not isinstance(value, bytes) or len(value) > 180_000:
            return None, "Görüntü boyutu geçersiz."
        try:
            picture = Image.open(io.BytesIO(value))
            if picture.format != "JPEG" or picture.width > 640 or picture.height > 480:
                return None, "En fazla 640×480 JPEG kare gerekli."
            picture = picture.convert("RGB")
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            return None, "Görüntü çözümlenemedi."
        path = model_path("face-detector") / "face_detection_yunet_2023mar.onnx"
        if not path.is_file():
            return None, "Yüz algılama modeli eksik (face-detector)."
        if self.face_detector is None:
            self.face_detector = cv2.FaceDetectorYN.create(str(path), "", (320, 320), .8, .3, 500)
        pixels = np.asarray(picture)
        gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
        # YuNet was trained for small faces. Detect at <=320px, crop the original.
        scale = min(1.0, 320 / max(picture.size))
        small = cv2.resize(cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR), (round(picture.width * scale), round(picture.height * scale)))
        self.face_detector.setInputSize((small.shape[1], small.shape[0]))
        _, detected = self.face_detector.detect(small)
        faces = [] if detected is None else detected
        if len(faces) != 1:
            return None, "Birden fazla yüz bulundu; yalnız kendi yüzünüz kadrajda olmalı." if len(faces) else "Kare ulaştı, yüz bulunamadı. Kendi kamera önizlemenizi ve aydınlatmayı kontrol edin."
        x, y, w, h = (float(v) / scale for v in faces[0][:4])
        left, top = max(0, int(x)), max(0, int(y))
        right, bottom = min(picture.width, int(x+w)), min(picture.height, int(y+h))
        if right - left < 48 or bottom - top < 48:
            return None, "Yüz çok küçük; kameraya biraz yaklaşın."
        crop = gray[top:bottom, left:right]
        if crop.mean() < 30 or crop.mean() > 235 or cv2.Laplacian(crop, cv2.CV_64F).var() < 20:
            return None, "Yüz karanlık, fazla aydınlık veya bulanık."
        return picture.crop((left, top, right, bottom)), ""


emotion_engine = EmotionEngine()
