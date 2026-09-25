"""Offline model smoke checks; these are execution tests, not accuracy evaluation."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

from django.core.management.base import BaseCommand, CommandError

from interviews.services.asr import asr_engine
from interviews.services.emotions import emotion_engine
from interviews.services.hf_runtime import device_name
from interviews.services.question_engine import question_engine


class Command(BaseCommand):
    help = "İndirilmiş model ağırlıklarını internete çıkmadan test eder."

    def add_arguments(self, parser):
        parser.add_argument("--audio", help="İsteğe bağlı, paylaşım izniniz olan test sesi")
        parser.add_argument("--report", help="JSON çalışma raporu dosyası")

    def handle(self, *args, **options):
        import numpy as np
        import torch
        from PIL import Image

        report = {"device": device_name(), "test_kind": "execution_only_not_accuracy", "checks": {}}
        failures = []
        for modality in ["text", "audio", "video"]:
            start = time.monotonic()
            try:
                processor, model = emotion_engine._load(modality)
                report["checks"][modality + "_load"] = {"ok": True, "ms": round((time.monotonic()-start)*1000)}
            except Exception as exc:
                failures.append(modality)
                report["checks"][modality + "_load"] = {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:300]}
                continue
            start = time.monotonic()
            if modality == "text":
                result = emotion_engine.analyze("text", "Bu projeyi tamamladığım için çok mutluyum.")
                ok = result["state"] in {"ok", "uncertain"} and len(result["scores"]) == 7
                report["checks"]["text_inference"] = {"ok": ok, "result": result}
            elif modality == "video":
                # Synthetic image tests model forward only, not face detection accuracy.
                inputs = processor(images=Image.new("RGB", (224, 224), (120, 100, 90)), return_tensors="pt")
                with torch.inference_mode():
                    logits = model(**{k: v.to(model.device) for k, v in inputs.items()}).logits
                ok = logits.shape == (1, 7) and bool(torch.isfinite(logits).all())
                report["checks"]["video_synthetic_forward"] = {"ok": ok, "ms": round((time.monotonic()-start)*1000)}
            else:
                signal = np.sin(np.arange(48000) * 2 * np.pi * 220 / 16000).astype(np.float32) * .1
                result = emotion_engine.analyze("audio", (signal * 32767).astype("<i2").tobytes())
                ok = result["state"] in {"ok", "uncertain"} and len(result["scores"]) == 4
                report["checks"]["audio_synthetic_forward"] = {"ok": ok, "ms": result.get("latency_ms"), "note": "Sinüs testi doğruluk ölçmez."}
            if not ok:
                failures.append(modality)

        if options["audio"]:
            import av
            chunks = []
            with av.open(options["audio"]) as container:
                resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
                for frame in container.decode(audio=0):
                    chunks.extend(f.to_ndarray().tobytes() for f in resampler.resample(frame))
                chunks.extend(f.to_ndarray().tobytes() for f in resampler.resample(None))
            pcm = b"".join(chunks)
            start = time.monotonic()
            result = asr_engine.transcribe_pcm(pcm)
            report["checks"]["asr_sample"] = {"ok": bool(result.text), "text": result.text, "audio_seconds": len(pcm)/32000, "ms": round((time.monotonic()-start)*1000)}
            report["checks"]["audio_sample"] = emotion_engine.analyze("audio", pcm[:96000])
            if not result.text:
                failures.append("asr")

        interview = SimpleNamespace(position="Backend geliştirici", job_description="Django ve Redis", rubric="Karar gerekçesi", resume_text="")
        segment = SimpleNamespace(span_id="utt_smoke123", speaker="candidate", is_final=True,
            text="Redis önbellek kullanarak yanıt süresini 800 milisaniyeden 200 milisaniyeye indirdim.", get_speaker_display=lambda: "Aday")
        start = time.monotonic()
        suggestions, provider = question_engine.suggest(interview, [segment])
        question_ok = bool(suggestions) and (question_engine.backend != "local" or provider.startswith("hf-qwen-"))
        report["checks"]["questions"] = {"ok": question_ok, "provider": provider, "questions": [q.text for q in suggestions], "ms": round((time.monotonic()-start)*1000)}
        if not question_ok:
            failures.append("questions")
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
        if options["report"]:
            Path(options["report"]).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        if failures:
            raise CommandError("Çalışmayan model kontrolleri: " + ", ".join(failures))
