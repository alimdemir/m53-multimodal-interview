import asyncio
import base64
import io
import json
import os
import threading
from types import SimpleNamespace
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from .models import Interview, TranscriptSegment
from .realtime import LiveAssistanceConsumer
from .services.asr import AsrResult
from .services.emotions import EmotionEngine
from .services.question_engine import QuestionEngine
from .services.local_questions import LocalQuestionModel
from .services.audio_signal import signal_metrics


class ModelContractTests(SimpleTestCase):
    def test_short_quiet_answer_is_not_diluted_by_surrounding_silence(self):
        import numpy as np
        import torch
        audio = np.zeros(48000, dtype=np.float32)
        audio[16000:20800] = .012 * np.sin(np.arange(4800) * 2 * np.pi * 220 / 16000)
        self.assertLess(float(np.sqrt(np.mean(audio ** 2))), .004)
        self.assertEqual(signal_metrics(audio)["active_ms"], 300)
        class Model:
            device = "cpu"
            config = SimpleNamespace(id2label={0: "Angry", 1: "Calm", 2: "Happy", 3: "Sad"})
            def __call__(self, **kwargs):
                return SimpleNamespace(logits=torch.zeros((1, 4)))
        with patch.dict(os.environ, {"EMOTION_ENABLED": "1"}):
            engine = EmotionEngine()
            with patch.object(engine, "_load", return_value=(lambda *a, **kw: {}, Model())) as load:
                result = engine.analyze("audio", (audio * 32767).astype("<i2").tobytes())
            self.assertEqual(result["state"], "uncertain")
            self.assertEqual(len(result["scores"]), 4)
            load.assert_called_once()

    def test_microphone_click_is_not_enough_audio_for_emotion(self):
        import numpy as np
        audio = np.zeros(48000, dtype="<i2")
        audio[1000:1160] = 16000
        with patch.dict(os.environ, {"EMOTION_ENABLED": "1"}):
            engine = EmotionEngine()
            with patch.object(engine, "_load") as load:
                self.assertEqual(engine.analyze("audio", audio.tobytes())["state"], "no_data")
            load.assert_not_called()

    def test_silence_never_loads_emotion_model(self):
        with patch.dict(os.environ, {"EMOTION_ENABLED": "1"}):
            engine = EmotionEngine()
            with patch.object(engine, "_load") as load:
                result = engine.analyze("audio", b"\0\0" * 48000)
            self.assertEqual(result["state"], "no_data")
            load.assert_not_called()

    def test_video_without_face_never_predicts(self):
        from PIL import Image
        image = Image.new("RGB", (320, 240), "white")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG")
        with patch.dict(os.environ, {"EMOTION_ENABLED": "1"}):
            engine = EmotionEngine()
            with patch.object(engine, "_load") as load:
                result = engine.analyze("video", buffer.getvalue())
            self.assertEqual(result["state"], "no_data")
            load.assert_not_called()

    def test_disabled_analysis_never_loads_weights(self):
        with patch.dict(os.environ, {"EMOTION_ENABLED": "0"}):
            engine = EmotionEngine()
            with patch.object(engine, "_load") as load:
                self.assertEqual(engine.analyze("text", "Çok mutluyum.")["state"], "disabled")
            load.assert_not_called()

    def test_bad_image_returns_no_data(self):
        with patch.dict(os.environ, {"EMOTION_ENABLED": "1"}):
            self.assertEqual(EmotionEngine().analyze("video", b"not a jpeg")["state"], "no_data")

    def test_face_crop_uses_detector_and_clamps_to_frame(self):
        import numpy as np
        from PIL import Image
        from unittest.mock import Mock
        pixels = np.random.default_rng(0).integers(60, 200, (240, 320, 3), dtype=np.uint8)
        image = io.BytesIO()
        Image.fromarray(pixels).save(image, format="JPEG")
        engine = EmotionEngine()
        engine.face_detector = Mock()
        engine.face_detector.detect.return_value = (None, np.array([[-10, -10, 100, 100] + [0]*10 + [.99]]))
        with patch("pathlib.Path.is_file", return_value=True):
            face, reason = engine._face(image.getvalue())
        self.assertEqual(face.size, (90, 90))
        self.assertEqual(reason, "")
        engine.face_detector.detect.return_value = (None, np.zeros((2, 15)))
        with patch("pathlib.Path.is_file", return_value=True):
            face, reason = engine._face(image.getvalue())
        self.assertIsNone(face)
        self.assertIn("Birden fazla", reason)

    def test_question_schema_rejects_invalid_evidence_and_sensitive_topics(self):
        engine = QuestionEngine()
        segment = SimpleNamespace(span_id="utt_valid")
        raw = {"questions": [
            {"text": "Neden?", "evidence_span_ids": ["utt_fake"]},
            {"text": "Stresli misiniz?", "evidence_span_ids": ["utt_valid"]},
            {"text": "Redis veri yapısını nasıl ölçtünüz?", "type": "evidence", "evidence_span_ids": ["utt_valid"]},
            {"text": "Önbelleği nasıl ölçtünüz?", "type": "evidence", "evidence_span_ids": ["utt_valid"]},
        ]}
        items = engine._parse_suggestions(json.dumps(raw), [segment])
        safe = [item for item in items if not item.prohibited_topic]
        self.assertEqual(len(safe), 1)
        self.assertEqual(safe[0].text, "Önbelleği nasıl ölçtünüz?")

    def test_question_evidence_is_limited_to_latest_candidate_turn(self):
        engine = QuestionEngine()
        old = SimpleNamespace(span_id="utt_old", speaker="candidate", is_final=True, text="Eski yanıt", get_speaker_display=lambda: "Aday")
        host = SimpleNamespace(span_id="utt_host", speaker="interviewer", is_final=True, text="Yeni soru", get_speaker_display=lambda: "Mülakatçı")
        current = SimpleNamespace(span_id="utt_new", speaker="candidate", is_final=True, text="Redis kullanarak gecikmeyi düşürdüm.", get_speaker_display=lambda: "Aday")
        raw = json.dumps({"questions": [{"text": "Redis etkisini hangi metrikle ölçtünüz?", "type": "evidence", "priority": 1, "rationale": "Yeni yanıt", "evidence_span_ids": ["utt_old"]}]})
        engine.backend = "local"
        interview = SimpleNamespace(position="Backend", job_description="", rubric="", resume_text="")
        with patch("interviews.services.question_engine.local_question_model.generate", return_value=raw):
            questions, provider = engine.suggest(interview, [old, host, current])
        self.assertEqual(provider, "rules")
        self.assertTrue(all(item.evidence_span_ids == ["utt_new"] for item in questions))

    def test_question_prompt_contains_only_current_turn(self):
        engine = QuestionEngine()
        old = SimpleNamespace(span_id="utt_old", speaker="candidate", is_final=True, text="Eski Redis cevabı", get_speaker_display=lambda: "Aday")
        host = SimpleNamespace(span_id="utt_host", speaker="interviewer", is_final=True, text="Veritabanında ne yaptınız?", get_speaker_display=lambda: "Görüşmeci")
        current = SimpleNamespace(span_id="utt_new", speaker="candidate", is_final=True, text="PostgreSQL indeksi ekledim.", get_speaker_display=lambda: "Aday")
        interview = SimpleNamespace(position="Backend", job_description="", rubric="", resume_text="")
        prompt = engine._messages(interview, [old, host, current], [current])[-1]["content"]
        self.assertNotIn("Eski Redis cevabı", prompt)
        self.assertIn("Veritabanında ne yaptınız?", prompt)
        self.assertIn("PostgreSQL indeksi ekledim.", prompt)

    def test_valid_model_json_with_trailing_quote_is_used_and_supplemented(self):
        engine = QuestionEngine()
        engine.backend = "local"
        current = SimpleNamespace(span_id="utt_new", speaker="candidate", is_final=True,
                                  text="PostgreSQL p95 sorgu süresini düşürdüm.", get_speaker_display=lambda: "Aday")
        interview = SimpleNamespace(position="Backend", job_description="", rubric="", resume_text="")
        raw = json.dumps({"questions": [{
            "text": "PostgreSQL p95 sonucunu hangi veri aralığında doğruladınız?",
            "type": "evidence", "priority": 1, "rationale": "Ölçümü netleştirir.",
            "evidence_span_ids": ["utt_new"],
        }]}) + '"'
        with patch("interviews.services.question_engine.local_question_model.generate", return_value=raw):
            questions, provider = engine.suggest(interview, [current])
        self.assertEqual(provider, "hf-qwen-mlx")
        self.assertEqual(len(questions), 2)
        self.assertTrue(all(item.evidence_span_ids == ["utt_new"] for item in questions))

    def test_near_duplicate_question_is_filtered(self):
        self.assertTrue(QuestionEngine._is_duplicate(
            "Bu çalışmanın sonucunu hangi metrikle ölçtünüz?",
            ["Bu çalışmanın sonucunu hangi metrik ile ölçtünüz?"],
        ))

    def test_question_does_not_reask_an_explicit_metric_or_technology(self):
        answer = "PostgreSQL sorgularında p95 süreyi 480 ms'den 120 ms'ye düşürdüm."
        self.assertTrue(QuestionEngine._reasks_explicit_fact(
            "Hangi metrikleri ve teknolojiyi kullandınız?", answer,
        ))
        self.assertFalse(QuestionEngine._reasks_explicit_fact(
            "PostgreSQL p95 düşüşünü hangi trafik koşulunda doğruladınız?", answer,
        ))

    def test_common_small_model_turkish_suffix_error_is_polished(self):
        raw = "P95 düşüşünden sonra, hangi ölçüme yöntemleri ile bu değişikliği doğruladınız?"
        self.assertEqual(
            QuestionEngine._polish_question(raw),
            "P95 düşüşünden sonra hangi ölçüm yöntemiyle bu değişikliği doğruladınız?",
        )

    def test_small_cpu_model_passive_question_is_polished(self):
        self.assertEqual(
            QuestionEngine._polish_question(
                "Redis önbellek ile hangi sorgu planı kanıtına göre kullanılmıştır?"
            ),
            "Redis önbelleğini hangi sorgu planı kanıtına göre kullandınız?",
        )

    def test_no_new_question_after_interviewer_starts_speaking(self):
        engine = QuestionEngine()
        engine.backend = "rules"
        segments = [SimpleNamespace(span_id="utt_candidate", speaker="candidate", is_final=True, text="Django kullandım."),
                    SimpleNamespace(span_id="utt_host", speaker="interviewer", is_final=True, text="Neden?")]
        questions, provider = engine.suggest(SimpleNamespace(), segments)
        self.assertEqual(provider, "rules")
        self.assertEqual(questions, [])

    def test_no_candidate_evidence_means_no_question(self):
        engine = QuestionEngine()
        questions, _ = engine.suggest(SimpleNamespace(), [SimpleNamespace(speaker="interviewer", is_final=True)])
        self.assertEqual(questions, [])

    def test_explicit_rules_backend_never_contacts_model(self):
        with patch.dict(os.environ, {"QUESTION_BACKEND": "rules"}):
            engine = QuestionEngine()
        segment = SimpleNamespace(span_id="utt_valid", speaker="candidate", is_final=True, text="Django projesinde Redis kullandım.")
        with patch("interviews.services.question_engine.local_question_model.generate") as generate:
            _, provider = engine.suggest(SimpleNamespace(), [segment])
        generate.assert_not_called()
        self.assertEqual(provider, "rules")
        self.assertEqual(engine.status()["state"], "fallback")

    def test_missing_local_model_falls_back_explicitly(self):
        engine = QuestionEngine()
        engine.backend = "local"
        segment = SimpleNamespace(span_id="utt_valid", speaker="candidate", is_final=True, text="Django projesinde Redis kullandım.", get_speaker_display=lambda: "Aday")
        interview = SimpleNamespace(position="Backend", job_description="", rubric="", resume_text="")
        with patch("interviews.services.question_engine.local_question_model.generate", side_effect=OSError("offline")):
            questions, provider = engine.suggest(interview, [segment])
        self.assertTrue(questions)
        self.assertEqual(provider, "rules")


class LocalQuestionThreadTests(SimpleTestCase):
    def test_different_request_threads_use_same_model_thread(self):
        model = LocalQuestionModel()
        self.addCleanup(model.worker.shutdown)
        with patch("interviews.services.local_questions.model_available", return_value=True), patch.object(model, "_generate_mlx", side_effect=lambda *a: threading.get_ident()):
            with ThreadPoolExecutor(max_workers=1) as first, ThreadPoolExecutor(max_workers=1) as second:
                one = first.submit(model.generate, []).result()
                two = second.submit(model.generate, []).result()
                self.assertEqual(one, two)
                self.assertNotEqual(one, threading.get_ident())

    def test_busy_model_does_not_queue_unbounded_requests_and_recovers(self):
        model = LocalQuestionModel()
        self.addCleanup(model.worker.shutdown)
        started, finish = threading.Event(), threading.Event()
        def slow(*args):
            started.set()
            finish.wait(2)
            return "ok"
        with patch("interviews.services.local_questions.model_available", return_value=True), patch.object(model, "_generate_mlx", side_effect=slow):
            with ThreadPoolExecutor(max_workers=1) as caller:
                pending = caller.submit(model.generate, [])
                self.assertTrue(started.wait(1))
                try:
                    with self.assertRaisesRegex(RuntimeError, "meşgul"):
                        model.generate([])
                finally:
                    finish.set()
                self.assertEqual(pending.result(), "ok")
                self.assertEqual(model.generate([]), "ok")

    def test_model_error_releases_admission_lock(self):
        model = LocalQuestionModel()
        self.addCleanup(model.worker.shutdown)
        with patch("interviews.services.local_questions.model_available", return_value=True), patch.object(model, "_generate_mlx", side_effect=[ValueError("test"), "ok"]):
            with self.assertRaises(ValueError):
                model.generate([])
            self.assertEqual(model.generate([]), "ok")

    def test_three_model_failures_open_short_circuit(self):
        model = LocalQuestionModel()
        self.addCleanup(model.worker.shutdown)
        with patch("interviews.services.local_questions.model_available", return_value=True), patch.object(model, "_generate_mlx", side_effect=ValueError("test")) as generate:
            for _ in range(3):
                with self.assertRaises(ValueError):
                    model.generate([])
            with self.assertRaisesRegex(RuntimeError, "dinlenmede"):
                model.generate([])
            self.assertEqual(generate.call_count, 3)


@override_settings(CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}})
class LiveAITests(TransactionTestCase):
    def setUp(self):
        self.host = get_user_model().objects.create_user("live-ai-host", password="unused-password")
        self.meeting = Interview.objects.create(created_by=self.host, title="Koçluk testi", position="Backend",
            candidate_name="Test", scheduled_at=timezone.now(), job_description="Django", research_mode=True)

    async def socket(self, role="candidate", consent=True, analysis=False):
        ws = WebsocketCommunicator(LiveAssistanceConsumer.as_asgi(), "/?role=" + role)
        ws.scope["url_route"] = {"kwargs": {"interview_id": str(self.meeting.id)}}
        ws.scope["session"] = {
            "joined_interviews": [str(self.meeting.id)] if consent else [],
            f"candidate_analysis_consent_{self.meeting.id}": bool(analysis),
        }
        ws.scope["user"] = self.host
        connected, code = await ws.connect()
        if connected:
            await ws.receive_json_from()
            await ws.receive_json_from()
        return ws, connected, code

    def test_guest_without_session_is_denied(self):
        async def run():
            ws, connected, code = await self.socket(consent=False)
            self.assertFalse(connected)
            self.assertEqual(code, 4403)
            await ws.disconnect()
        async_to_sync(run)()

    def test_normal_interview_cannot_enable_emotions(self):
        self.meeting.research_mode = False
        self.meeting.save()
        async def run():
            ws, _, _ = await self.socket(analysis=True)
            await ws.send_json_to({"type": "analysis-consent", "enabled": True})
            response = await ws.receive_json_from()
            self.assertFalse(response["enabled"])
            await ws.disconnect()
        with patch.dict(os.environ, {"EMOTION_ENABLED": "1"}):
            async_to_sync(run)()

    def test_candidate_result_is_visible_only_to_host(self):
        async def run():
            candidate, _, _ = await self.socket(analysis=True)
            host, _, _ = await self.socket("host")
            status = await host.receive_json_from()
            self.assertEqual(status["type"], "candidate-analysis-status")
            self.assertTrue(status["enabled"])
            await candidate.send_json_to({"type": "frame", "jpeg": base64.b64encode(b"test").decode()})
            result = await host.receive_json_from()
            self.assertEqual(result["type"], "candidate-emotion")
            self.assertTrue(await candidate.receive_nothing(.1))
            await candidate.disconnect()
            await host.disconnect()
        with patch.dict(os.environ, {"EMOTION_ENABLED": "1"}), patch("interviews.realtime.emotion_engine.analyze", return_value={"modality": "video", "state": "ok", "scores": []}):
            async_to_sync(run)()

    def test_frames_without_optin_are_not_processed(self):
        async def run():
            ws, _, _ = await self.socket()
            await ws.send_json_to({"type": "frame", "jpeg": base64.b64encode(b"test").decode()})
            self.assertTrue(await ws.receive_nothing(.1))
            await ws.disconnect()
        with patch("interviews.realtime.emotion_engine.analyze") as analyze:
            async_to_sync(run)()
            analyze.assert_not_called()

    def test_captions_reach_both_participants(self):
        async def run():
            candidate, _, _ = await self.socket()
            host, _, _ = await self.socket("host")
            await candidate.send_to(bytes_data=b"\x01\x00" * 48000)
            result = await host.receive_json_from(timeout=3)
            while result["type"] != "transcript":
                result = await host.receive_json_from(timeout=3)
            self.assertEqual(result["type"], "transcript")
            self.assertEqual(result["speaker"], "candidate")
            own_messages = [await candidate.receive_json_from(), await candidate.receive_json_from()]
            self.assertIn("transcript", [message["type"] for message in own_messages])
            await candidate.disconnect()
            await host.disconnect()
        with patch.dict(os.environ, {"ASR_ENABLED": "1"}), patch("interviews.realtime.asr_engine.transcribe_pcm", return_value=AsrResult("Django kullandım.", .8, "tr", 3)):
            async_to_sync(run)()
        self.assertEqual(self.meeting.transcript_segments.count(), 1)

    def test_first_short_answer_reaches_audio_and_text_without_six_second_skip(self):
        async def run():
            candidate, _, _ = await self.socket(analysis=True)
            host, _, _ = await self.socket("host")
            await candidate.send_to(bytes_data=b"\x01\x00" * 48000)
            modalities = set()
            transcript_seen = False
            while len(modalities) < 2 or not transcript_seen:
                result = await host.receive_json_from(timeout=3)
                if result["type"] == "candidate-emotion":
                    modalities.add(result["modality"])
                if result["type"] == "transcript":
                    transcript_seen = True
            self.assertEqual(modalities, {"audio", "text"})
            own_messages = [await candidate.receive_json_from(), await candidate.receive_json_from()]
            self.assertNotIn("candidate-emotion", [message["type"] for message in own_messages])
            await candidate.disconnect()
            await host.disconnect()
        with patch.dict(os.environ, {"ASR_ENABLED": "1", "EMOTION_ENABLED": "1"}), patch("interviews.realtime.asr_engine.transcribe_pcm", return_value=AsrResult("Django kullandım.", .8, "tr", 3)), patch("interviews.realtime.emotion_engine.analyze", side_effect=lambda modality, value: {"modality": modality, "state": "uncertain", "scores": []}):
            async_to_sync(run)()

    def test_latest_pending_analysis_is_bounded_and_not_lost(self):
        async def run():
            consumer = LiveAssistanceConsumer()
            consumer.tasks, consumer.pending_analysis = {}, {}
            consumer.analysis_consent, consumer.closed, consumer.epoch = True, False, 1
            started, finish = asyncio.Event(), asyncio.Event()
            values = []
            async def analyze(modality, value, epoch):
                values.append(value)
                started.set()
                await finish.wait()
            consumer._analyze = analyze
            consumer.queue_analysis("text", "first", 1)
            await started.wait()
            consumer.queue_analysis("text", "superseded", 1)
            consumer.queue_analysis("text", "latest", 1)
            self.assertEqual(len(consumer.pending_analysis), 1)
            finish.set()
            await consumer.tasks["text"]
            self.assertEqual(values, ["first", "latest"])
        async_to_sync(run)()

    def test_revocation_drops_inflight_prediction(self):
        started, finish = threading.Event(), threading.Event()
        def slow(*args):
            started.set()
            finish.wait(timeout=3)
            return {"modality": "video", "state": "ok", "scores": []}
        async def run():
            ws, _, _ = await self.socket(analysis=True)
            await ws.send_json_to({"type": "frame", "jpeg": base64.b64encode(b"test").decode()})
            await asyncio.to_thread(started.wait, 2)
            await ws.send_json_to({"type": "analysis-consent", "enabled": False})
            self.assertFalse((await ws.receive_json_from())["enabled"])
            finish.set()
            self.assertTrue(await ws.receive_nothing(.15))
            await ws.disconnect()
        with patch.dict(os.environ, {"EMOTION_ENABLED": "1"}), patch("interviews.realtime.emotion_engine.analyze", side_effect=slow):
            async_to_sync(run)()

    def test_oversized_pcm_rejected(self):
        async def run():
            ws, _, _ = await self.socket()
            await ws.send_to(bytes_data=b"\0" * 100000)
            response = await ws.receive_output()
            self.assertEqual(response["type"], "websocket.close")
            self.assertEqual(response["code"], 4400)
            await ws.disconnect()
        async_to_sync(run)()

    def test_malformed_json_handled(self):
        async def run():
            ws, _, _ = await self.socket()
            await ws.send_json_to(["not", "an", "object"])
            self.assertEqual((await ws.receive_json_from())["type"], "error")
            await ws.disconnect()
        async_to_sync(run)()

    def test_flush_transcribes_subsecond_tail_with_real_timestamps(self):
        async def run():
            ws, _, _ = await self.socket()
            await ws.send_to(bytes_data=b"\x01\x00" * 8000)
            self.assertTrue(await ws.receive_nothing(.05))
            await ws.send_json_to({"type": "flush", "request_id": "tail-test"})
            while True:
                result = await ws.receive_json_from(timeout=3)
                if result["type"] == "flushed":
                    self.assertEqual(result["request_id"], "tail-test")
                    self.assertEqual(result["dropped_audio_seconds"], 0)
                    break
            await ws.disconnect()
        with patch.dict(os.environ, {"ASR_ENABLED": "1"}), patch("interviews.realtime.asr_engine.transcribe_pcm", return_value=AsrResult("Teşekkürler.", .8, "tr", 1)) as asr:
            async_to_sync(run)()
            self.assertEqual(len(asr.call_args.args[0]), 32000)
        segment = self.meeting.transcript_segments.get()
        self.assertEqual(segment.ended_ms - segment.started_ms, 500)

    def test_flush_during_inflight_asr_drains_remaining_audio(self):
        started, finish = threading.Event(), threading.Event()
        calls = []
        def slow(pcm, rate):
            calls.append(len(pcm))
            if len(calls) == 1:
                started.set()
                finish.wait(timeout=3)
            return AsrResult("Test cümlesi.", .8, "tr", len(pcm)/32000)
        async def run():
            ws, _, _ = await self.socket()
            await ws.send_to(bytes_data=b"\x01\x00" * 48000)
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            await ws.send_to(bytes_data=b"\x01\x00" * 8000)
            await ws.send_json_to({"type": "flush", "request_id": "busy-flush"})
            finish.set()
            while (await ws.receive_json_from(timeout=3))["type"] != "flushed":
                pass
            await ws.disconnect()
        with patch.dict(os.environ, {"ASR_ENABLED": "1"}), patch("interviews.realtime.asr_engine.transcribe_pcm", side_effect=slow):
            async_to_sync(run)()
        self.assertEqual(calls, [96000, 32000])
        self.assertEqual(self.meeting.transcript_segments.count(), 2)
