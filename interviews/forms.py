from django import forms
from django.utils import timezone

from .models import Interview


class InterviewForm(forms.ModelForm):
    scheduled_at = forms.DateTimeField(
        label="Tarih ve saat",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        input_formats=["%Y-%m-%dT%H:%M"],
    )

    class Meta:
        model = Interview
        fields = [
            "title",
            "position",
            "candidate_name",
            "candidate_email",
            "scheduled_at",
            "duration_minutes",
            "job_description",
            "rubric",
            "resume_text",
            "retention_days",
            "research_mode",
        ]
        labels = {
            "title": "Toplantı başlığı",
            "position": "Pozisyon",
            "candidate_name": "Adayın adı",
            "candidate_email": "Adayın e-postası (isteğe bağlı)",
            "duration_minutes": "Süre (dakika)",
            "job_description": "İş ilanı / rol beklentileri",
            "rubric": "Değerlendirme rubriği",
            "resume_text": "CV metni",
            "retention_days": "Transkript saklama süresi (gün)",
            "research_mode": "Koçluk / deneme oturumu (işe alım değerlendirmesi için değil)",
        }
        help_texts = {"research_mode": "Aday ayrıca izin verirse deneysel ses, metin ve yüz ifadesi sinyalleri yalnız mülakatçı ekranında anlık gösterilir. Sonuçlar kaydedilmez ve otomatik karara aktarılmaz."}
        widgets = {
            "job_description": forms.Textarea(attrs={"rows": 6}),
            "rubric": forms.Textarea(attrs={"rows": 4}),
            "resume_text": forms.Textarea(attrs={"rows": 6}),
            "duration_minutes": forms.NumberInput(attrs={"min": 15, "max": 180}),
            "retention_days": forms.NumberInput(attrs={"min": 1, "max": 365}),
        }

    def clean_scheduled_at(self):
        value = self.cleaned_data["scheduled_at"]
        if value < timezone.now() - timezone.timedelta(hours=1):
            raise forms.ValidationError("Toplantı zamanı geçmişte olamaz.")
        return value


class ConsentForm(forms.Form):
    participant_name = forms.CharField(label="Adınız", max_length=160)
    informed_consent = forms.BooleanField(
        label="Aydınlatma metnini okudum ve sesimin canlı transkripsiyon için işlenmesini kabul ediyorum."
    )
    camera_optional = forms.BooleanField(
        label="Kameranın isteğe bağlı olduğunu ve kapatmamın değerlendirmeyi olumsuz etkilemeyeceğini anladım."
    )
    analysis_consent = forms.BooleanField(
        required=False,
        label="Deneysel ses, metin ve yüz ifadesi sinyallerimin yalnız mülakatçı ekranında anlık gösterilmesini kabul ediyorum. Sonuçların kaydedilmediğini ve otomatik karar için kullanılmadığını anladım.",
    )

    def __init__(self, *args, research_mode=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not research_mode:
            self.fields.pop("analysis_consent", None)
