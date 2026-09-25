"""Ephemeral PCM quality measurements, not speech/emotion probabilities."""
import math


def signal_metrics(audio, sample_rate=16000):
    import numpy as np

    frame_size = sample_rate // 50  # 20 ms; pauses must not dilute short answers.
    rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
    complete = audio[:audio.size // frame_size * frame_size]
    frames = complete.reshape(-1, frame_size)
    active = int(np.count_nonzero(np.sqrt(np.mean(frames ** 2, axis=1)) >= .004)) if frames.size else 0
    return {
        "duration_ms": round(audio.size * 1000 / sample_rate),
        "rms_dbfs": round(20 * math.log10(max(rms, 1e-6)), 1),
        "active_ms": active * 20,
        "clipped_fraction": float(np.mean(np.abs(audio) > .98)) if audio.size else 0.0,
    }
