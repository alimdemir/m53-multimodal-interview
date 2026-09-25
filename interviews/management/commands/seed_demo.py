import secrets

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from interviews.models import Interview


class Command(BaseCommand):
    help = "Yerel tanıtım için bir mülakatçı hesabı ve örnek toplantı oluşturur."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="demo")
        parser.add_argument("--email", default="demo@localhost")
        parser.add_argument("--password", default="")
        parser.add_argument("--without-sample", action="store_true")

    def handle(self, *args, **options):
        user_model = get_user_model()
        password = options["password"] or secrets.token_urlsafe(14)
        user, created = user_model.objects.get_or_create(
            username=options["username"],
            defaults={
                "email": options["email"],
                "first_name": "Demo",
                "last_name": "Mülakatçı",
                "is_staff": True,
            },
        )
        if created or options["password"]:
            user.set_password(password)
            user.save()

        if not options["without_sample"] and not user.interviews.exists():
            Interview.objects.create(
                created_by=user,
                title="Kıdemli Backend Geliştirici Görüşmesi",
                position="Senior Backend Developer",
                candidate_name="Deniz Yılmaz",
                candidate_email="deniz@example.com",
                scheduled_at=timezone.now() + timezone.timedelta(days=1),
                duration_minutes=45,
                job_description=(
                    "Django/Python ile yüksek trafikli servis geliştirme; PostgreSQL ve Redis; "
                    "gözlemlenebilirlik, güvenlik, test ve ekip içi teknik liderlik."
                ),
                rubric=(
                    "Sistem tasarımı; ölçülebilir etki; teknik ödünleşimler; hata yönetimi; "
                    "açık iletişim ve iş birliği."
                ),
                resume_text=(
                    "5 yıl Python/Django deneyimi. Sipariş altyapısında servis ayrıştırma projesi. "
                    "PostgreSQL performans iyileştirmeleri ve ekip içi mentorluk."
                ),
            )

        if created or options["password"]:
            self.stdout.write(self.style.SUCCESS(f"Kullanıcı: {user.username}"))
            self.stdout.write(self.style.WARNING(f"Tek kullanımlık yerel demo parolası: {password}"))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"{user.username} zaten var; parola değiştirilmedi. --password ile değiştirebilirsiniz."
                )
            )
