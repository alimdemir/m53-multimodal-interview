import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


def new_span_id() -> str:
    return f"utt_{secrets.token_hex(6)}"


class Interview(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Taslak"
        SCHEDULED = "scheduled", "Planlandı"
        LIVE = "live", "Canlı"
        COMPLETED = "completed", "Tamamlandı"
        CANCELLED = "cancelled", "İptal"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    guest_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="interviews",
    )
    title = models.CharField(max_length=160)
    position = models.CharField(max_length=160)
    candidate_name = models.CharField(max_length=160)
    candidate_email = models.EmailField(blank=True)
    scheduled_at = models.DateTimeField()
    duration_minutes = models.PositiveSmallIntegerField(default=45)
    job_description = models.TextField()
    rubric = models.TextField(blank=True)
    resume_text = models.TextField(blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.SCHEDULED,
        db_index=True,
    )
    recording_enabled = models.BooleanField(default=False)
    research_mode = models.BooleanField(default=False)
    retention_days = models.PositiveSmallIntegerField(default=30)
    consent_version = models.CharField(max_length=32, default="v1.0")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-scheduled_at"]

    def __str__(self) -> str:
        return f"{self.title} · {self.candidate_name}"

    @property
    def is_joinable(self) -> bool:
        return self.status not in {self.Status.COMPLETED, self.Status.CANCELLED}

    def mark_live(self) -> None:
        if self.status in {self.Status.DRAFT, self.Status.SCHEDULED}:
            self.status = self.Status.LIVE
            self.started_at = self.started_at or timezone.now()
            self.save(update_fields=["status", "started_at", "updated_at"])


class ConsentRecord(models.Model):
    interview = models.ForeignKey(
        Interview, on_delete=models.CASCADE, related_name="consents"
    )
    participant_name = models.CharField(max_length=160)
    version = models.CharField(max_length=32)
    accepted = models.BooleanField(default=True)
    camera_optional_acknowledged = models.BooleanField(default=False)
    recording_acknowledged = models.BooleanField(default=False)
    analysis_acknowledged = models.BooleanField(default=False)
    ip_hash = models.CharField(max_length=64, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class TranscriptSegment(models.Model):
    class Speaker(models.TextChoices):
        CANDIDATE = "candidate", "Aday"
        INTERVIEWER = "interviewer", "Mülakatçı"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    span_id = models.CharField(max_length=32, default=new_span_id, unique=True)
    interview = models.ForeignKey(
        Interview, on_delete=models.CASCADE, related_name="transcript_segments"
    )
    speaker = models.CharField(max_length=16, choices=Speaker.choices)
    text = models.TextField()
    is_final = models.BooleanField(default=True)
    started_ms = models.PositiveBigIntegerField(default=0)
    ended_ms = models.PositiveBigIntegerField(default=0)
    confidence = models.FloatField(null=True, blank=True)
    source = models.CharField(max_length=24, default="whisper")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["interview", "created_at"])]


class SuggestedQuestion(models.Model):
    class QuestionType(models.TextChoices):
        CLARIFY = "clarify", "Netleştirme"
        DEPTH = "depth", "Teknik derinlik"
        EVIDENCE = "evidence", "Kanıt"
        TRADEOFF = "tradeoff", "Takas/tercih"
        REFLECTION = "reflection", "Değerlendirme"
        COMPETENCY_GAP = "competency_gap", "Yeterlik boşluğu"

    class State(models.TextChoices):
        SUGGESTED = "suggested", "Önerildi"
        ASKED = "asked", "Soruldu"
        DISMISSED = "dismissed", "Reddedildi"
        SUPERSEDED = "superseded", "Yeni yanıtla güncellendi"

    interview = models.ForeignKey(
        Interview, on_delete=models.CASCADE, related_name="suggested_questions"
    )
    text = models.CharField(max_length=500)
    question_type = models.CharField(max_length=24, choices=QuestionType.choices)
    priority = models.PositiveSmallIntegerField(default=2)
    rationale = models.CharField(max_length=500)
    evidence_span_ids = models.JSONField(default=list)
    prohibited_topic = models.BooleanField(default=False)
    state = models.CharField(
        max_length=16, choices=State.choices, default=State.SUGGESTED
    )
    provider = models.CharField(max_length=48, default="rules")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["priority", "-created_at"]


class AuditEvent(models.Model):
    interview = models.ForeignKey(
        Interview, on_delete=models.CASCADE, related_name="audit_events"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    event_type = models.CharField(max_length=64)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
