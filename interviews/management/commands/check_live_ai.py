"""Consent-gated live-pipeline smoke; isolated fixtures, no real meeting writes."""
import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.core.management.base import BaseCommand, CommandError
from django.test import override_settings
from django.utils import timezone

from interviews.realtime import LiveAssistanceConsumer
from interviews.services.emotions import emotion_engine


class Command(BaseCommand):
    help = "İzinli bir test sesini gerçek ASR ve duygu modelleriyle canlı soket akışından geçirir; veritabanına yazmaz."

    def add_arguments(self, parser):
        parser.add_argument("--audio", required=True, help="Paylaşım izniniz olan veya sentetik test sesi")

    def handle(self, *args, **options):
        import av
        from PIL import Image
        chunks = []
        with av.open(options["audio"]) as container:
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            for frame in container.decode(audio=0):
                chunks.extend(f.to_ndarray().tobytes() for f in resampler.resample(frame))
            chunks.extend(f.to_ndarray().tobytes() for f in resampler.resample(None))
        pcm = b"".join(chunks)[:96000]
        if len(pcm) < 96000:
            raise CommandError("En az 3 saniyelik konuşma içeren test sesi gerekli.")
        fixture = SimpleNamespace(id="00000000-0000-0000-0000-000000000001", research_mode=True, started_at=timezone.now(), created_at=timezone.now())

        async def authorize(consumer):
            consumer.interview_id = fixture.id
            consumer.role = "candidate"
            return True

        async def save(consumer, result, started_ms, ended_ms):
            return SimpleNamespace(span_id="utt_smoke", speaker="candidate", text=result.text,
                started_ms=started_ms, ended_ms=ended_ms, confidence=result.confidence,
                get_speaker_display=lambda: "Sentetik test")

        async def publish(consumer, result):
            await consumer.emit({"type": "candidate-emotion", **result})

        async def run():
            ws = WebsocketCommunicator(LiveAssistanceConsumer.as_asgi(), "/?role=candidate")
            ws.scope["session"] = {f"candidate_analysis_consent_{fixture.id}": True}
            report = {"kind": "real_models_synthetic_audio_no_browser_capture", "audio": False, "text": False, "transcript": False}
            try:
                connected, _ = await ws.connect()
                if not connected:
                    raise CommandError("Test soketi bağlanamadı.")
                await ws.receive_json_from()
                await ws.receive_json_from()
                # One short answer immediately after connection: previously skipped.
                await ws.send_to(bytes_data=pcm)
                while not all(report[key] for key in ("audio", "text", "transcript")):
                    message = await ws.receive_json_from(timeout=40)
                    if message["type"] == "transcript":
                        report["transcript"] = bool(message["text"])
                    elif message["type"] == "candidate-emotion":
                        modality = message["modality"]
                        if message["state"] not in {"ok", "uncertain"}:
                            raise CommandError(f"{modality}: {message['state']} · {message.get('detail', '')}")
                        report[modality] = len(message["scores"]) > 0
                        report[modality + "_ms"] = message.get("latency_ms")
            finally:
                await ws.disconnect(timeout=5)
            # A blank camera image must not produce a facial-expression label.
            picture = io.BytesIO()
            Image.new("RGB", (320, 240), "white").save(picture, format="JPEG")
            blank = await asyncio.to_thread(emotion_engine.analyze, "video", picture.getvalue())
            report["blank_frame_rejected"] = blank["state"] == "no_data" and "yüz bulunamadı" in blank["detail"]
            if not report["blank_frame_rejected"]:
                raise CommandError("Yüz algılayıcı boş kare kontrolü başarısız.")
            return report

        with override_settings(CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}), patch.object(LiveAssistanceConsumer, "authorize", authorize), patch.object(LiveAssistanceConsumer, "_mark_live", new=AsyncMock()), patch.object(LiveAssistanceConsumer, "_get_interview", new=AsyncMock(return_value=fixture)), patch.object(LiveAssistanceConsumer, "_still_live", new=AsyncMock(return_value=True)), patch.object(LiveAssistanceConsumer, "_audit_consent", new=AsyncMock()), patch.object(LiveAssistanceConsumer, "_save_segment", save), patch.object(LiveAssistanceConsumer, "_publish_candidate_emotion", publish):
            report = async_to_sync(run)()
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
