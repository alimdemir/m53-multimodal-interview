"""Bounded live captions and consent-gated, ephemeral candidate signals."""
import asyncio
import base64
import binascii
import json
import time

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone

from .consumers import InterviewAccessMixin
from .models import AuditEvent, ConsentRecord, Interview, TranscriptSegment
from .services.asr import asr_engine
from .services.emotions import emotion_engine


class LiveAssistanceConsumer(InterviewAccessMixin, AsyncWebsocketConsumer):
    sample_rate = 16000
    window_bytes = 16000 * 2 * 3
    max_buffer_bytes = window_bytes * 3

    async def connect(self):
        self.closed = False
        self.epoch = 0
        if not await self.authorize():
            await self.close(code=4403)
            return
        await self._mark_live()
        self.interview = await self._get_interview()
        self.group_name = f"transcript_{self.interview_id.replace('-', '')}"
        self.buffer = bytearray()
        self.tasks = {}
        self.pending_analysis = {}
        session = self.scope.get("session", {})
        self.analysis_authorized = bool(
            self.role == "candidate"
            and session.get(f"candidate_analysis_consent_{self.interview_id}", False)
        )
        self.analysis_consent = bool(
            self.analysis_authorized
            and self.interview.research_mode
            and emotion_engine.enabled
        )
        self.epoch = 0
        self.closed = False
        self.last_live_check = 0
        self.last_frame = 0
        self.last_analysis = {"audio": 0, "text": 0}
        self.preconsent_bytes = 0
        self.last_consent_change = 0
        self.ended_ms = 0
        self.flush_requested = False
        self.flush_id = ""
        self.capture_finished = False
        self.dropped_bytes = 0
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self.emit({"type": "service-status", "service": "asr", **asr_engine.status()})
        if self.role == "host":
            candidate_allowed = bool(
                self.interview.research_mode
                and emotion_engine.enabled
                and await self._candidate_has_analysis_consent()
            )
            await self.emit({"type": "analysis-status", "enabled": False,
                             "allowed": candidate_allowed, "source": "candidate",
                             "models": emotion_engine.status()})
            await self.channel_layer.group_send(self.group_name, {
                "type": "candidate.analysis.request", "sender_channel": self.channel_name,
            })
        else:
            await self.emit({"type": "analysis-status", "enabled": self.analysis_consent,
                             "allowed": self.analysis_authorized and self.interview.research_mode and emotion_engine.enabled,
                             "source": "candidate", "models": emotion_engine.status()})
            await self._publish_candidate_analysis_status()

    async def emit(self, payload):
        if not self.closed:
            await self.send(text_data=json.dumps(payload, ensure_ascii=False))

    async def disconnect(self, close_code):
        if hasattr(self, "group_name") and getattr(self, "role", "") == "candidate":
            self.analysis_consent = False
            await self._publish_candidate_analysis_status()
        self.closed = True
        self.epoch = getattr(self, "epoch", 0) + 1
        self.analysis_consent = False
        if hasattr(self, "buffer"):
            self.buffer.clear()
        getattr(self, "pending_analysis", {}).clear()
        tasks = list(getattr(self, "tasks", {}).values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    def launch(self, name, coroutine_factory):
        task = self.tasks.get(name)
        if task is None or task.done():
            self.tasks[name] = asyncio.create_task(coroutine_factory())
            return True
        return False

    def queue_analysis(self, modality, value, epoch):
        # At most one running + one latest pending window per modality/socket.
        # Do not discard a short answer just because the previous model is busy.
        self.pending_analysis[modality] = (value, epoch)
        self.launch(modality, lambda: self._drain_analysis(modality))

    async def _drain_analysis(self, modality):
        while modality in self.pending_analysis and self.analysis_consent and not self.closed:
            value, epoch = self.pending_analysis.pop(modality)
            if epoch == self.epoch:
                await self._analyze(modality, value, epoch)

    async def receive(self, text_data=None, bytes_data=None):
        now = time.monotonic()
        if now - self.last_live_check > 1:
            self.last_live_check = now
            if not await self._still_live():
                await self.close(code=4403)
                return
        if text_data is not None:
            if len(text_data) > 245_000:
                await self.close(code=4400)
                return
            try:
                payload = json.loads(text_data)
                if not isinstance(payload, dict):
                    raise ValueError()
            except (ValueError, TypeError):
                await self.emit({"type": "error", "message": "Geçersiz ileti."})
                return
            kind = payload.get("type")
            if self.capture_finished and kind not in {"flush", "ping"}:
                return
            if kind == "analysis-consent":
                # Revocation is always immediate; rate-limit only repeated opt-ins.
                if payload.get("enabled") is True and now - self.last_consent_change < 2:
                    await self.emit({"type": "analysis-status", "enabled": self.analysis_consent,
                                     "allowed": self.analysis_authorized and self.interview.research_mode and emotion_engine.enabled,
                                     "models": emotion_engine.status()})
                    return
                self.last_consent_change = now
                self.epoch += 1
                self.analysis_consent = bool(
                    payload.get("enabled") is True
                    and self.role == "candidate"
                    and self.analysis_authorized
                    and self.interview.research_mode
                    and emotion_engine.enabled
                )
                self.pending_analysis.clear()
                if self.analysis_consent:
                    self.last_analysis = {"audio": 0, "text": 0}
                # Audio captured before opt-in still goes to captions, never coaching.
                self.preconsent_bytes = len(self.buffer)
                await self._audit_consent()
                await self.emit({"type": "analysis-status", "enabled": self.analysis_consent,
                                 "allowed": self.analysis_authorized and self.interview.research_mode and emotion_engine.enabled,
                                 "models": emotion_engine.status()})
                if self.role == "candidate":
                    await self._publish_candidate_analysis_status()
            elif kind == "frame" and self.analysis_consent and now - self.last_frame >= 4:
                encoded = payload.get("jpeg", "")
                if not isinstance(encoded, str) or len(encoded) > 240_000:
                    return
                try:
                    picture = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error):
                    return
                self.last_frame = now
                epoch = self.epoch
                self.queue_analysis("video", picture, epoch)
            elif kind == "media-state" and payload.get("camera") is False and self.analysis_consent:
                # Invalidates in-flight video to prevent stale labels after camera-off.
                self.epoch += 1
                await self._publish_candidate_emotion(
                    emotion_engine.unavailable("video", "Kamera kapalı.")
                )
            elif kind == "flush":
                self.flush_requested = True
                self.flush_id = str(payload.get("request_id", ""))[:48]
                self.capture_finished = True
                self.analysis_consent = False
                self.pending_analysis.clear()
                self.epoch += 1
                self.launch("asr", self._drain)
            elif kind == "ping":
                await self.emit({"type": "pong"})
            return
        if not bytes_data or self.capture_finished:
            return
        if len(bytes_data) > self.window_bytes or len(bytes_data) % 2:
            await self.close(code=4400)
            return
        if not asr_engine.enabled and not self.analysis_consent:
            return
        self.buffer.extend(bytes_data)
        if len(self.buffer) > self.max_buffer_bytes:
            overflow = len(self.buffer) - self.max_buffer_bytes
            self.dropped_bytes += overflow
            del self.buffer[:overflow]
            await self.emit({"type": "service-status", "service": "asr", "state": "degraded",
                             "label": "Altyazı gecikiyor · eski ses düşürüldü"})
        if len(self.buffer) >= self.window_bytes:
            self.launch("asr", self._drain)

    async def _drain(self):
        while len(self.buffer) >= (2 if self.flush_requested else self.window_bytes) and not self.closed:
            amount = min(len(self.buffer), self.window_bytes)
            # Timeline uses server receive time; missing/muted audio is not compressed.
            elapsed = max(0, int((timezone.now() - (self.interview.started_at or self.interview.created_at)).total_seconds() * 1000))
            started_ms = max(self.ended_ms, elapsed - int(len(self.buffer) / 32))
            self.ended_ms = started_ms + int(amount / 32)
            chunk = bytes(self.buffer[:amount])
            chunk_epoch = self.epoch if self.analysis_consent and not self.preconsent_bytes else None
            self.preconsent_bytes = max(0, self.preconsent_bytes - amount)
            del self.buffer[:amount]
            now = time.monotonic()
            if chunk_epoch is not None and now - self.last_analysis["audio"] >= 6:
                self.last_analysis["audio"] = now
                self.queue_analysis("audio", chunk, chunk_epoch)
            if not asr_engine.enabled:
                continue
            try:
                # Preserve even the final sub-second tail. Padding is inference-only;
                # stored timestamps always reflect the actual captured duration.
                inference_chunk = chunk.ljust(32000, b"\0") if self.flush_requested else chunk
                result = await asyncio.to_thread(asr_engine.transcribe_pcm, inference_chunk, self.sample_rate)
            except Exception:
                self.dropped_bytes += amount
                await self.emit({"type": "service-status", "service": "asr", "state": "error",
                                 "label": "Altyazı işlenemedi · sonraki pencere beklenecek"})
                continue
            if self.closed or not result.text:
                continue
            segment = await self._save_segment(result, started_ms, self.ended_ms)
            if segment is None:
                await self.close(code=4403)
                return
            await self.channel_layer.group_send(self.group_name, {
                "type": "transcript.message", "payload": {
                    "type": "transcript", "span_id": segment.span_id, "speaker": segment.speaker,
                    "speaker_label": segment.get_speaker_display(), "text": segment.text,
                    "is_final": True, "started_ms": segment.started_ms, "ended_ms": segment.ended_ms,
                    "confidence": segment.confidence,
                },
            })
            await self.emit({"type": "service-status", "service": "asr", "state": "ready", "label": "Türkçe altyazı canlı"})
            if (self.analysis_consent and chunk_epoch == self.epoch and
                    time.monotonic() - self.last_analysis["text"] >= 6):
                self.last_analysis["text"] = time.monotonic()
                self.queue_analysis("text", result.text, self.epoch)
        if self.flush_requested and not self.closed:
            self.flush_requested = False
            await self.emit({"type": "flushed", "request_id": self.flush_id,
                             "dropped_audio_seconds": round(self.dropped_bytes / 32000, 3)})

    async def _analyze(self, modality, value, epoch):
        result = await asyncio.to_thread(emotion_engine.analyze, modality, value)
        # The consent epoch prevents late results after revoke/re-enable or camera-off.
        if self.analysis_consent and epoch == self.epoch and not self.closed:
            if await self._still_live():
                await self._publish_candidate_emotion(result)

    async def _publish_candidate_emotion(self, result):
        if self.role != "candidate" or not self.analysis_consent:
            return
        await self.channel_layer.group_send(self.group_name, {
            "type": "candidate.emotion",
            "sender_channel": self.channel_name,
            "payload": {"type": "candidate-emotion", **result},
        })

    async def _publish_candidate_analysis_status(self):
        await self.channel_layer.group_send(self.group_name, {
            "type": "candidate.analysis.status",
            "sender_channel": self.channel_name,
            "payload": {
                "type": "candidate-analysis-status",
                "enabled": bool(self.analysis_consent),
                "allowed": bool(self.analysis_authorized and self.interview.research_mode and emotion_engine.enabled),
            },
        })

    @database_sync_to_async
    def _still_live(self):
        return Interview.objects.filter(pk=self.interview_id).exclude(status__in=["completed", "cancelled"]).exists()

    @database_sync_to_async
    def _candidate_has_analysis_consent(self):
        value = ConsentRecord.objects.filter(interview_id=self.interview_id).order_by(
            "-created_at"
        ).values_list("analysis_acknowledged", flat=True).first()
        return bool(value)

    @database_sync_to_async
    def _audit_consent(self):
        AuditEvent.objects.create(interview_id=self.interview_id, event_type="coaching.consent",
                                  metadata={"role": self.role, "accepted": self.analysis_consent, "version": "coaching-v2", "scope": "candidate_stream_host_visible_ephemeral"})

    @database_sync_to_async
    def _save_segment(self, result, started_ms, ended_ms):
        if not Interview.objects.filter(pk=self.interview_id).exclude(status__in=["completed", "cancelled"]).exists():
            return None
        return TranscriptSegment.objects.create(
            interview_id=self.interview_id, speaker="candidate" if self.role == "candidate" else "interviewer",
            text=result.text[:10000], started_ms=started_ms, ended_ms=ended_ms, confidence=result.confidence,
        )

    async def transcript_message(self, event):
        await self.emit(event["payload"])

    async def candidate_analysis_request(self, event):
        if self.role == "candidate" and event.get("sender_channel") != self.channel_name:
            await self._publish_candidate_analysis_status()

    async def candidate_analysis_status(self, event):
        if self.role == "host" and event.get("sender_channel") != self.channel_name:
            await self.emit(event["payload"])

    async def candidate_emotion(self, event):
        if self.role == "host" and event.get("sender_channel") != self.channel_name:
            await self.emit(event["payload"])

    async def room_closed(self, event):
        self.analysis_consent = False
        self.epoch += 1
        await self.emit({"type": "room-closed"})
        await self.close(code=1000)
