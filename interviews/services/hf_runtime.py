"""Offline-only model loading. Downloads are an explicit administrative step."""
import os
import threading
from pathlib import Path

from django.conf import settings


def model_path(key):
    value = os.getenv(f"{key.upper().replace('-', '_')}_MODEL_PATH", f"models/{key}")
    path = Path(value)
    return path if path.is_absolute() else settings.BASE_DIR / path


def model_available(key):
    path = model_path(key)
    return (path / "config.json").is_file() and any(path.glob("*.safetensors"))


def device_name():
    import torch

    requested = os.getenv("HF_DEVICE", "auto")
    if requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda"
    return "mps" if torch.backends.mps.is_available() else "cpu"


# One heavy emotion inference at a time per process; never build an unbounded queue.
emotion_slot = threading.Lock()
