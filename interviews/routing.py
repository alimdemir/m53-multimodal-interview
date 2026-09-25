from django.urls import re_path

from . import consumers
from .realtime import LiveAssistanceConsumer


websocket_urlpatterns = [
    re_path(
        r"^ws/interviews/(?P<interview_id>[0-9a-f-]+)/signal/$",
        consumers.SignalingConsumer.as_asgi(),
    ),
    re_path(
        r"^ws/interviews/(?P<interview_id>[0-9a-f-]+)/asr/$",
        LiveAssistanceConsumer.as_asgi(),
    ),
]
