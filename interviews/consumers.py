import json
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.utils import timezone

from .models import AuditEvent, Interview


def _query_role(scope) -> str:
    query = parse_qs(scope.get("query_string", b"").decode("utf-8"))
    return query.get("role", [""])[0]


class InterviewAccessMixin:
    async def authorize(self) -> bool:
        self.interview_id = str(self.scope["url_route"]["kwargs"]["interview_id"])
        self.role = _query_role(self.scope)
        self.interview = await self._get_interview()
        if self.interview is None or not self.interview.is_joinable:
            return False
        if self.role == "host":
            user = self.scope.get("user")
            return bool(user and user.is_authenticated and user.id == self.interview.created_by_id)
        if self.role == "candidate":
            joined = self.scope.get("session", {}).get("joined_interviews", [])
            return self.interview_id in joined
        return False

    @database_sync_to_async
    def _get_interview(self):
        try:
            return Interview.objects.get(pk=self.interview_id)
        except (Interview.DoesNotExist, ValueError):
            return None

    @database_sync_to_async
    def _mark_live(self):
        interview = Interview.objects.get(pk=self.interview_id)
        if interview.status in {Interview.Status.DRAFT, Interview.Status.SCHEDULED}:
            interview.status = Interview.Status.LIVE
            interview.started_at = interview.started_at or timezone.now()
            interview.save(update_fields=["status", "started_at", "updated_at"])
            AuditEvent.objects.create(
                interview=interview,
                event_type="interview.started",
                metadata={"trigger_role": self.role},
            )


class SignalingConsumer(InterviewAccessMixin, AsyncJsonWebsocketConsumer):
    allowed_types = {"offer", "answer", "ice", "presence", "ready", "media-state", "leave", "ping", "prepare-close", "capture-flushed"}

    async def connect(self):
        if not await self.authorize():
            await self.close(code=4403)
            return
        self.group_name = f"interview_{self.interview_id.replace('-', '')}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self._mark_live()
        await self.channel_layer.group_send(
            self.group_name,
            {
                "type": "signal.message",
                "sender_channel": self.channel_name,
                "payload": {"type": "presence", "state": "joined", "role": self.role},
            },
        )

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_send(
                self.group_name,
                {
                    "type": "signal.message",
                    "sender_channel": self.channel_name,
                    "payload": {"type": "presence", "state": "left", "role": getattr(self, "role", "")},
                },
            )
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive_json(self, content, **kwargs):
        if not isinstance(content, dict):
            await self.close(code=4400)
            return
        message_type = content.get("type")
        if message_type == "prepare-close" and self.role != "host":
            return
        if message_type not in self.allowed_types:
            await self.send_json({"type": "error", "message": "Geçersiz sinyal türü."})
            return
        encoded = json.dumps(content)
        if len(encoded) > 64_000:
            await self.send_json({"type": "error", "message": "Sinyal iletisi çok büyük."})
            return
        content["role"] = self.role
        await self.channel_layer.group_send(
            self.group_name,
            {"type": "signal.message", "sender_channel": self.channel_name, "payload": content},
        )

    async def signal_message(self, event):
        if event["sender_channel"] != self.channel_name:
            await self.send_json(event["payload"])
