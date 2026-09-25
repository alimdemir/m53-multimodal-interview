from django.contrib import admin

from .models import AuditEvent, ConsentRecord, Interview, SuggestedQuestion, TranscriptSegment


@admin.register(Interview)
class InterviewAdmin(admin.ModelAdmin):
    list_display = ("title", "candidate_name", "position", "scheduled_at", "status")
    list_filter = ("status", "scheduled_at")
    search_fields = ("title", "candidate_name", "candidate_email", "position")
    readonly_fields = ("id", "guest_token", "created_at", "updated_at")


@admin.register(ConsentRecord)
class ConsentRecordAdmin(admin.ModelAdmin):
    list_display = ("participant_name", "interview", "version", "created_at")
    readonly_fields = ("ip_hash", "user_agent", "created_at")


admin.site.register(TranscriptSegment)
admin.site.register(SuggestedQuestion)
admin.site.register(AuditEvent)
