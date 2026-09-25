import uuid
import json
from unittest.mock import patch

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Interview, SuggestedQuestion, TranscriptSegment
from .services.question_engine import QuestionEngine, question_engine
from config.asgi import application


@override_settings(
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
)
class InterviewFlowTests(TestCase):
    def setUp(self):
        # HTTP/DB tests must not launch optional real models from the user's .env.
        backend = patch.object(question_engine, "backend", "rules")
        backend.start()
        self.addCleanup(backend.stop)
        user_model = get_user_model()
        self.host = user_model.objects.create_user("host", password="valid-test-password")
        self.outsider = user_model.objects.create_user("other", password="valid-test-password")
        self.interview = Interview.objects.create(
            created_by=self.host,
            title="Backend görüşmesi",
            position="Backend Developer",
            candidate_name="Ada Aday",
            candidate_email="ada@example.com",
            scheduled_at=timezone.now() + timezone.timedelta(days=1),
            duration_minutes=45,
            job_description="Python, Django, PostgreSQL ve ölçülebilir sistem etkisi.",
            rubric="Teknik derinlik, karar gerekçesi ve öğrenme.",
            resume_text="Django ile sipariş servisi geliştirdi.",
        )

    def test_host_sees_meeting_and_invite(self):
        self.client.force_login(self.host)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Backend görüşmesi")
        self.assertContains(response, str(self.interview.guest_token))

    def test_other_user_cannot_open_host_pages(self):
        self.client.force_login(self.outsider)
        response = self.client.get(reverse("interview_detail", args=[self.interview.id]))
        self.assertEqual(response.status_code, 404)

    def test_candidate_must_consent_before_room(self):
        room_url = reverse("candidate_room", args=[self.interview.id])
        response = self.client.get(room_url)
        self.assertRedirects(
            response,
            reverse("candidate_join", args=[self.interview.guest_token]),
            fetch_redirect_response=False,
        )
        response = self.client.post(
            reverse("candidate_join", args=[self.interview.guest_token]),
            {
                "participant_name": "Ada Aday",
                "informed_consent": "on",
                "camera_optional": "on",
            },
        )
        self.assertRedirects(response, room_url, fetch_redirect_response=False)
        self.assertEqual(self.interview.consents.count(), 1)
        self.assertIn(str(self.interview.id), self.client.session["joined_interviews"])
        self.assertEqual(self.client.get(room_url).status_code, 200)

    def test_candidate_analysis_is_separate_consent_and_stays_off_candidate_ui(self):
        self.interview.research_mode = True
        self.interview.save()
        response = self.client.post(
            reverse("candidate_join", args=[self.interview.guest_token]),
            {
                "participant_name": "Ada Aday",
                "informed_consent": "on",
                "camera_optional": "on",
                "analysis_consent": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        consent = self.interview.consents.get()
        self.assertTrue(consent.analysis_acknowledged)
        self.assertTrue(
            self.client.session[f"candidate_analysis_consent_{self.interview.id}"]
        )
        room = self.client.get(reverse("candidate_room", args=[self.interview.id]))
        self.assertNotContains(room, 'id="emotion-overlay"')
        self.assertNotContains(room, 'id="live-captions"')

    def test_rotating_invite_invalidates_old_token(self):
        old_token = self.interview.guest_token
        self.client.force_login(self.host)
        self.client.post(reverse("rotate_invite", args=[self.interview.id]))
        self.interview.refresh_from_db()
        self.assertNotEqual(old_token, self.interview.guest_token)
        self.assertEqual(
            self.client.get(reverse("candidate_join", args=[old_token])).status_code,
            404,
        )

    def test_completion_closes_candidate_link(self):
        self.client.force_login(self.host)
        response = self.client.post(reverse("complete_interview", args=[self.interview.id]))
        self.assertEqual(response.status_code, 200)
        self.interview.refresh_from_db()
        self.assertEqual(self.interview.status, Interview.Status.COMPLETED)
        self.client.logout()
        response = self.client.get(reverse("candidate_join", args=[self.interview.guest_token]))
        self.assertEqual(response.status_code, 410)

    def test_question_endpoint_requires_final_evidence(self):
        segment = TranscriptSegment.objects.create(
            interview=self.interview,
            speaker=TranscriptSegment.Speaker.CANDIDATE,
            text="Django ile sipariş servisini yeniden tasarladım.",
            is_final=True,
        )
        self.client.force_login(self.host)
        response = self.client.post(reverse("generate_questions", args=[self.interview.id]))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertGreaterEqual(len(body["questions"]), 1)
        self.assertTrue(
            all(segment.span_id in item["evidence_span_ids"] for item in body["questions"])
        )
        self.assertFalse(
            SuggestedQuestion.objects.filter(prohibited_topic=True).exists()
        )

    def test_prohibited_topic_filter(self):
        self.assertTrue(QuestionEngine.is_prohibited("Evli misiniz?"))
        self.assertTrue(QuestionEngine.is_prohibited("Herhangi bir sağlık sorununuz var mı?"))
        self.assertFalse(QuestionEngine.is_prohibited("Bu performans sorununu nasıl çözdünüz?"))
        self.assertFalse(QuestionEngine.is_prohibited("Yaşadığınız teknik problemi anlatır mısınız?"))

    def test_host_tools_are_compact_overlays_without_side_panel(self):
        self.client.force_login(self.host)
        url = reverse("host_room", args=[self.interview.id])
        self.assertNotContains(self.client.get(url), 'id="analysis-consent"')
        self.interview.research_mode = True
        self.interview.save()
        response = self.client.get(url)
        self.assertContains(response, 'id="toggle-captions"')
        self.assertContains(response, 'class="question-overlay"')
        self.assertContains(response, 'id="emotion-overlay"')
        self.assertContains(response, "Aday duygu sinyalleri")
        self.assertContains(response, 'id="live-captions"')
        self.assertContains(response, 'data-view-mode="gallery"')
        self.assertContains(response, 'data-view-mode="speaker"')
        self.assertNotContains(response, 'id="room-side-panel"')
        self.assertNotContains(response, 'id="coaching-disclosure"')
        self.assertNotContains(response, 'id="analysis-consent"')
        self.assertNotContains(response, 'id="transcript-list"')

    def test_candidate_room_contains_no_interviewer_overlays(self):
        self.interview.research_mode = True
        self.interview.save()
        self.client.post(
            reverse("candidate_join", args=[self.interview.guest_token]),
            {"participant_name": "Ada Aday", "informed_consent": "on", "camera_optional": "on"},
        )
        response = self.client.get(reverse("candidate_room", args=[self.interview.id]))
        self.assertContains(response, 'id="local-video"')
        self.assertContains(response, 'id="remote-video"')
        self.assertContains(response, 'id="toggle-mic"')
        self.assertContains(response, 'data-view-mode="speaker"')
        self.assertNotContains(response, 'id="room-side-panel"')
        self.assertNotContains(response, 'id="coaching-disclosure"')
        self.assertNotContains(response, 'id="analysis-consent"')
        self.assertNotContains(response, 'id="emotion-overlay"')
        self.assertNotContains(response, 'id="toggle-captions"')
        self.assertNotContains(response, 'id="live-captions"')
        self.assertNotContains(response, 'id="question-list"')
        self.assertNotContains(response, 'id="transcript-list"')

    def test_repeated_questions_for_same_evidence_are_not_duplicated(self):
        TranscriptSegment.objects.create(interview=self.interview, speaker="candidate", text="Django ile servis geliştirdim.")
        self.client.force_login(self.host)
        url = reverse("generate_questions", args=[self.interview.id])
        self.client.post(url)
        count = self.interview.suggested_questions.count()
        self.client.post(url)
        self.assertEqual(self.interview.suggested_questions.count(), count)

    def test_unchanged_candidate_evidence_does_not_run_local_model_twice(self):
        segment = TranscriptSegment.objects.create(interview=self.interview, speaker="candidate", text="Redis kullanarak yanıt süresini düşürdüm.")
        self.client.force_login(self.host)
        url = reverse("generate_questions", args=[self.interview.id])
        payload = json.dumps({"questions": [{
            "text": "Redis etkisini hangi ölçümle doğruladınız?", "type": "evidence", "priority": 1,
            "rationale": "Ölçülebilir etkiyi doğrular.", "evidence_span_ids": [segment.span_id],
        }]})
        with patch.object(question_engine, "backend", "local"), patch("interviews.services.question_engine.local_question_model.generate", return_value=payload) as generate:
            first = self.client.post(url)
            second = self.client.post(url)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(generate.call_count, 1)
        self.assertTrue(second.json()["reused"])
        self.assertEqual(second.json()["questions"][0]["evidence"][0]["text"], segment.text)

    def test_same_generic_rule_is_not_stored_again_for_new_span(self):
        TranscriptSegment.objects.create(interview=self.interview, speaker="candidate", text="Django ile bir servis geliştirdim.")
        self.client.force_login(self.host)
        url = reverse("generate_questions", args=[self.interview.id])
        self.client.post(url)
        first_count = self.interview.suggested_questions.count()
        TranscriptSegment.objects.create(interview=self.interview, speaker="candidate", text="Sonra bu servisi üretime aldım.")
        self.client.post(url)
        self.assertEqual(self.interview.suggested_questions.count(), first_count)

    def test_new_answer_supersedes_previous_visible_question_group(self):
        first = TranscriptSegment.objects.create(interview=self.interview, speaker="candidate", text="Redis ile gecikmeyi ölçülebilir biçimde azalttım.")
        self.client.force_login(self.host)
        url = reverse("generate_questions", args=[self.interview.id])
        first_payload = json.dumps({"questions": [{
            "text": "Redis etkisini hangi ölçümle doğruladınız?", "type": "evidence", "priority": 1,
            "rationale": "İlk yanıt", "evidence_span_ids": [first.span_id],
        }]})
        with patch.object(question_engine, "backend", "local"), patch("interviews.services.question_engine.local_question_model.generate", return_value=first_payload):
            self.client.post(url)
        old = self.interview.suggested_questions.get(text="Redis etkisini hangi ölçümle doğruladınız?")
        TranscriptSegment.objects.create(interview=self.interview, speaker="interviewer", text="Veritabanında ne yaptınız?")
        second = TranscriptSegment.objects.create(interview=self.interview, speaker="candidate", text="PostgreSQL birleşik indeksini sorgu planına göre ekledim.")
        second_payload = json.dumps({"questions": [{
            "text": "PostgreSQL indeksini hangi sorgu planıyla doğruladınız?", "type": "depth", "priority": 1,
            "rationale": "Yeni yanıt", "evidence_span_ids": [second.span_id],
        }]})
        with patch.object(question_engine, "backend", "local"), patch("interviews.services.question_engine.local_question_model.generate", return_value=second_payload):
            response = self.client.post(url)
        old.refresh_from_db()
        self.assertEqual(old.state, SuggestedQuestion.State.SUPERSEDED)
        self.assertEqual(response.json()["questions"][0]["evidence"][0]["span_id"], second.span_id)
        self.assertEqual(
            self.interview.suggested_questions.filter(state=SuggestedQuestion.State.SUGGESTED).count(),
            len(response.json()["questions"]),
        )

    def test_completed_meeting_does_not_generate_questions(self):
        self.interview.status = Interview.Status.COMPLETED
        self.interview.save()
        self.client.force_login(self.host)
        self.assertEqual(self.client.post(reverse("generate_questions", args=[self.interview.id])).status_code, 410)

    def test_invalid_uuid_has_no_access(self):
        self.client.force_login(self.host)
        response = self.client.get(reverse("interview_detail", args=[uuid.uuid4()]))
        self.assertEqual(response.status_code, 404)


@override_settings(
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
    ALLOWED_HOSTS=["testserver"],
)
class WebSocketAccessTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        user = get_user_model().objects.create_user("ws-host", password="valid-test-password")
        self.interview = Interview.objects.create(
            created_by=user,
            title="WebSocket görüşmesi",
            position="Developer",
            candidate_name="Aday",
            scheduled_at=timezone.now() + timezone.timedelta(days=1),
            job_description="Python",
        )

    def test_candidate_socket_requires_consent_session_and_marks_live(self):
        async_to_sync(self._exercise_socket)()
        self.interview.refresh_from_db()
        self.assertEqual(self.interview.status, Interview.Status.LIVE)
        self.assertTrue(
            self.interview.audit_events.filter(event_type="interview.started").exists()
        )

    async def _exercise_socket(self):
        denied = WebsocketCommunicator(
            application,
            f"/ws/interviews/{self.interview.id}/signal/?role=candidate",
            headers=[(b"origin", b"http://testserver")],
        )
        connected, code = await denied.connect()
        self.assertFalse(connected)
        self.assertEqual(code, 4403)

        session = SessionStore()
        session["joined_interviews"] = [str(self.interview.id)]
        await session.asave()
        allowed = WebsocketCommunicator(
            application,
            f"/ws/interviews/{self.interview.id}/signal/?role=candidate",
            headers=[
                (b"origin", b"http://testserver"),
                (b"cookie", f"sessionid={session.session_key}".encode("ascii")),
            ],
        )
        connected, _ = await allowed.connect()
        self.assertTrue(connected)
        await allowed.disconnect()
