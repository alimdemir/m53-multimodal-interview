import hashlib
import json
import uuid
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.templatetags.static import static
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .forms import ConsentForm, InterviewForm
from .models import AuditEvent, ConsentRecord, Interview, SuggestedQuestion, TranscriptSegment
from .services.asr import asr_engine
from .services.question_engine import question_engine
from .services.emotions import emotion_engine


def home(request):
    return redirect("dashboard" if request.user.is_authenticated else "login")


def _host_interview_or_404(request, interview_id):
    return get_object_or_404(Interview, pk=interview_id, created_by=request.user)


def _join_url(request, interview):
    return request.build_absolute_uri(reverse("candidate_join", args=[interview.guest_token]))


@login_required
def dashboard(request):
    interviews = list(request.user.interviews.all())
    for interview in interviews:
        interview.join_url = _join_url(request, interview)
    return render(
        request,
        "interviews/dashboard.html",
        {
            "interviews": interviews,
            "scheduled_count": sum(
                interview.status == Interview.Status.SCHEDULED for interview in interviews
            ),
            "completed_count": sum(
                interview.status == Interview.Status.COMPLETED for interview in interviews
            ),
        },
    )


@login_required
def interview_create(request):
    initial = {"scheduled_at": timezone.localtime() + timezone.timedelta(days=1)}
    form = InterviewForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        interview = form.save(commit=False)
        interview.created_by = request.user
        interview.save()
        AuditEvent.objects.create(
            interview=interview,
            actor=request.user,
            event_type="interview.created",
            metadata={"status": interview.status},
        )
        messages.success(request, "Mülakat oluşturuldu. Davet bağlantısı paylaşılmaya hazır.")
        return redirect("interview_detail", interview_id=interview.id)
    return render(request, "interviews/interview_form.html", {"form": form})


@login_required
def interview_detail(request, interview_id):
    interview = _host_interview_or_404(request, interview_id)
    return render(
        request,
        "interviews/interview_detail.html",
        {
            "interview": interview,
            "join_url": _join_url(request, interview),
            "consent": interview.consents.first(),
        },
    )


@login_required
@require_POST
def rotate_invite(request, interview_id):
    interview = _host_interview_or_404(request, interview_id)
    interview.guest_token = uuid.uuid4()
    interview.save(update_fields=["guest_token", "updated_at"])
    AuditEvent.objects.create(
        interview=interview,
        actor=request.user,
        event_type="invite.rotated",
    )
    messages.success(request, "Eski bağlantı iptal edildi ve yeni davet bağlantısı oluşturuldu.")
    return redirect("interview_detail", interview_id=interview.id)


def candidate_join(request, token):
    interview = get_object_or_404(Interview, guest_token=token)
    if not interview.is_joinable:
        return render(request, "interviews/join_closed.html", {"interview": interview}, status=410)
    form = ConsentForm(
        request.POST or None,
        initial={"participant_name": interview.candidate_name},
        research_mode=interview.research_mode,
    )
    if request.method == "POST" and form.is_valid():
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        remote_ip = forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR", "")
        ip_hash = hashlib.sha256(
            f"{settings.SECRET_KEY}:{remote_ip}".encode("utf-8")
        ).hexdigest()
        ConsentRecord.objects.create(
            interview=interview,
            participant_name=form.cleaned_data["participant_name"],
            version=interview.consent_version,
            accepted=True,
            camera_optional_acknowledged=form.cleaned_data["camera_optional"],
            recording_acknowledged=not interview.recording_enabled,
            analysis_acknowledged=bool(form.cleaned_data.get("analysis_consent")),
            ip_hash=ip_hash,
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:300],
        )
        joined = set(request.session.get("joined_interviews", []))
        joined.add(str(interview.id))
        request.session["joined_interviews"] = sorted(joined)
        request.session[f"participant_name_{interview.id}"] = form.cleaned_data[
            "participant_name"
        ]
        request.session[f"candidate_analysis_consent_{interview.id}"] = bool(
            form.cleaned_data.get("analysis_consent")
        )
        request.session.cycle_key()
        AuditEvent.objects.create(
            interview=interview,
            event_type="consent.accepted",
            metadata={
                "version": interview.consent_version,
                "analysis_acknowledged": bool(form.cleaned_data.get("analysis_consent")),
            },
        )
        return redirect("candidate_room", interview_id=interview.id)
    return render(
        request,
        "interviews/candidate_join.html",
        {"interview": interview, "form": form},
    )


def _room_context(request, interview, role):
    join_url = _join_url(request, interview) if role == "host" else ""
    room_config = {
        "interviewId": str(interview.id),
        "role": role,
        "signalPath": f"/ws/interviews/{interview.id}/signal/?role={role}",
        "asrPath": f"/ws/interviews/{interview.id}/asr/?role={role}",
        "iceServers": settings.ICE_SERVERS,
        "joinUrl": join_url,
        "researchMode": interview.research_mode,
        "emotionAvailable": emotion_engine.enabled,
        "candidateAnalysisConsent": bool(
            role == "candidate"
            and request.session.get(f"candidate_analysis_consent_{interview.id}", False)
        ),
        "audioWorkletUrl": static("js/audio-capture.js"),
        "questionsUrl": reverse("generate_questions", args=[interview.id])
        if role == "host"
        else "",
        "completeUrl": reverse("complete_interview", args=[interview.id])
        if role == "host"
        else "",
    }
    transcript_segments = list(interview.transcript_segments.filter(is_final=True).order_by(
        "-created_at"
    )[:100])[::-1]
    segment_map = {segment.span_id: segment.text for segment in transcript_segments}
    latest_candidate = next((segment for segment in reversed(transcript_segments) if segment.speaker == "candidate"), None)
    all_question_pool = list(interview.suggested_questions.filter(
        prohibited_topic=False,
    ).order_by("-created_at")[:50])
    question_pool = [question for question in all_question_pool if question.state == SuggestedQuestion.State.SUGGESTED]
    if latest_candidate:
        current = [question for question in question_pool if latest_candidate.span_id in question.evidence_span_ids]
        latest_was_processed = any(latest_candidate.span_id in question.evidence_span_ids for question in all_question_pool)
        question_pool = current if latest_was_processed else question_pool
    suggested_questions = []
    seen_questions = set()
    for question in question_pool:
        normalized = question_engine._normalized_question(question.text)
        if normalized in seen_questions:
            continue
        seen_questions.add(normalized)
        suggested_questions.append(question)
        if len(suggested_questions) == 2:
            break
    return {
        "interview": interview,
        "role": role,
        "is_host": role == "host",
        "join_url": join_url,
        "room_config": room_config,
        "transcript_segments": transcript_segments,
        "suggested_question_cards": [
            {"question": question, "evidence": [
                {"span_id": span_id, "text": segment_map.get(span_id, "İlgili yanıta git")[:140]}
                for span_id in question.evidence_span_ids
            ]}
            for question in suggested_questions
        ],
        "asr_status": asr_engine.status(),
        "question_status": question_engine.status(),
        "emotion_modalities": [("text", "Metin", "Türkçe BERT"), ("audio", "Ses", "Türkçe HuBERT"), ("video", "Görüntü", "Yüz ifadesi ViT")],
    }


@login_required
def host_room(request, interview_id):
    interview = _host_interview_or_404(request, interview_id)
    if not interview.is_joinable:
        messages.info(request, "Bu mülakat artık katılıma açık değil.")
        return redirect("interview_detail", interview_id=interview.id)
    return render(
        request,
        "interviews/room.html",
        _room_context(request, interview, "host"),
    )


def candidate_room(request, interview_id):
    interview = get_object_or_404(Interview, pk=interview_id)
    joined = request.session.get("joined_interviews", [])
    if str(interview.id) not in joined:
        return redirect("candidate_join", token=interview.guest_token)
    if not interview.is_joinable:
        return render(request, "interviews/join_closed.html", {"interview": interview}, status=410)
    return render(
        request,
        "interviews/room.html",
        _room_context(request, interview, "candidate"),
    )


@login_required
@require_POST
def generate_questions(request, interview_id):
    interview = _host_interview_or_404(request, interview_id)
    if not interview.is_joinable:
        return JsonResponse({"error": "Görüşme kapalı."}, status=410)
    key = f"question-inflight:{interview.id}"
    if not cache.add(key, True, timeout=60):
        response = JsonResponse({"error": "Soru hazırlanıyor; biraz sonra tekrar deneyin."}, status=429)
        response["Retry-After"] = "15"
        return response
    try:
        return _generate_question_response(request, interview, force=request.GET.get("force") == "1")
    finally:
        cache.delete(key)


def _serialize_question(item, segment_map):
    return {
        "id": item.id,
        "text": item.text,
        "type": item.question_type,
        "type_label": item.get_question_type_display(),
        "priority": item.priority,
        "rationale": item.rationale,
        "evidence_span_ids": item.evidence_span_ids,
        "evidence": [
            {"span_id": span_id, "text": segment_map.get(span_id, "İlgili yanıta git")[:140]}
            for span_id in item.evidence_span_ids
        ],
        "provider": item.provider,
    }


def _generate_question_response(request, interview, force=False):
    segments = list(
        interview.transcript_segments.filter(is_final=True).order_by("-created_at")[:20]
    )[::-1]
    segment_map = {segment.span_id: segment.text for segment in segments}
    latest_candidate = next((segment for segment in reversed(segments) if segment.speaker == "candidate"), None)
    recent_questions = list(interview.suggested_questions.filter(prohibited_topic=False).order_by("-created_at")[:40])
    if latest_candidate and not force:
        processed = [item for item in recent_questions if latest_candidate.span_id in item.evidence_span_ids]
        if processed:
            active = [item for item in processed if item.state == SuggestedQuestion.State.SUGGESTED][:2]
            provider = processed[0].provider
            return JsonResponse({"questions": [_serialize_question(item, segment_map) for item in active],
                                 "provider": provider, "reused": True})
    previous_texts = list(interview.suggested_questions.filter(prohibited_topic=False)
                          .order_by("-created_at").values_list("text", flat=True)[:200])
    suggestions, provider = question_engine.suggest(interview, segments, previous_texts)
    created = []
    for suggestion in suggestions:
        if suggestion.prohibited_topic:
            continue
        item, _ = SuggestedQuestion.objects.get_or_create(
            interview=interview,
            text=suggestion.text,
            evidence_span_ids=suggestion.evidence_span_ids,
            defaults=dict(
                question_type=suggestion.question_type,
                priority=suggestion.priority,
                rationale=suggestion.rationale,
                prohibited_topic=False,
                provider=provider,
            ),
        )
        if item.state != SuggestedQuestion.State.SUGGESTED:
            continue
        created.append(_serialize_question(item, segment_map))
    if created:
        current_ids = [item["id"] for item in created]
        interview.suggested_questions.filter(
            state=SuggestedQuestion.State.SUGGESTED,
        ).exclude(pk__in=current_ids).update(state=SuggestedQuestion.State.SUPERSEDED)
    AuditEvent.objects.create(
        interview=interview,
        actor=request.user,
        event_type="questions.generated",
        metadata={"provider": provider, "count": len(created)},
    )
    return JsonResponse({"questions": created[:2], "provider": provider, "reused": False})


@login_required
@require_POST
def question_state(request, interview_id, question_id):
    interview = _host_interview_or_404(request, interview_id)
    question = get_object_or_404(SuggestedQuestion, pk=question_id, interview=interview)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Geçersiz JSON."}, status=400)
    state = payload.get("state")
    if state not in {SuggestedQuestion.State.ASKED, SuggestedQuestion.State.DISMISSED}:
        return JsonResponse({"error": "Geçersiz durum."}, status=400)
    question.state = state
    question.save(update_fields=["state"])
    AuditEvent.objects.create(
        interview=interview,
        actor=request.user,
        event_type=f"question.{state}",
        metadata={"question_id": question.id, "evidence_span_ids": question.evidence_span_ids},
    )
    return JsonResponse({"ok": True, "state": state})


@login_required
@require_POST
def complete_interview(request, interview_id):
    interview = _host_interview_or_404(request, interview_id)
    interview.status = Interview.Status.COMPLETED
    interview.ended_at = timezone.now()
    interview.save(update_fields=["status", "ended_at", "updated_at"])
    async_to_sync(get_channel_layer().group_send)(
        f"transcript_{str(interview.id).replace('-', '')}", {"type": "room.closed"}
    )
    AuditEvent.objects.create(
        interview=interview,
        actor=request.user,
        event_type="interview.completed",
    )
    messages.success(request, "Mülakat tamamlandı ve davet bağlantısı kapatıldı.")
    return JsonResponse({"ok": True, "redirect": reverse("interview_detail", args=[interview.id])})


@require_GET
def service_status(request):
    return JsonResponse(
        {
            "application": {"state": "ready", "label": "Django hazır"},
            "asr": asr_engine.status(),
            "questions": question_engine.status(),
            "emotions": emotion_engine.status(),
        }
    )
