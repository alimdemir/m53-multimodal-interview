from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse
from django.utils import timezone

from interviews.models import Interview


class Command(BaseCommand):
    help = "Mevcut hesabı değiştirmeden ayrı, isteğe bağlı bir koçluk demosu oluşturur."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="demo")

    def handle(self, *args, **options):
        user = get_user_model().objects.filter(username=options["username"]).first()
        if not user:
            raise CommandError("Önce bir mülakatçı hesabı oluşturun.")
        meeting, _ = Interview.objects.get_or_create(
            created_by=user, title="Hugging Face · Kişisel Koçluk Demosu", research_mode=True,
            status=Interview.Status.SCHEDULED,
            defaults={"candidate_name": "Deneme Katılımcısı", "position": "Teknik görüşme provası",
                      "scheduled_at": timezone.now() + timezone.timedelta(hours=1),
                      "job_description": "Python, Django ve sistem tasarımı üzerine görüşme provası.",
                      "rubric": "Soru önerileri yalnız konuşma içeriğine dayanır."},
        )
        self.stdout.write("Katılımcı: " + reverse("candidate_join", args=[meeting.guest_token]))
        self.stdout.write("Görüşmeci: " + reverse("host_room", args=[meeting.id]))
