"""
Audio transcription via OpenAI Whisper.
Used when Teams native transcription is unavailable (bot-recorded meetings).
"""
import logging
import os
import tempfile
from typing import Optional

from core.config import OPENAI_API_KEY, WHISPER_MODEL

log = logging.getLogger("summarizer.transcribe")

# Whisper's maximum file size is 25 MB.  ACS WAV recordings can be larger
# for long meetings, so we chunk by time if needed.  For now we rely on
# the caller to split very long recordings; most meetings fit under 25 MB.
_WHISPER_MAX_BYTES = 24 * 1024 * 1024   # 24 MB (leave 1 MB headroom)


def transcribe_audio(audio_bytes: bytes, filename: str = "recording.wav") -> Optional[str]:
    """
    Transcribe raw audio bytes using OpenAI Whisper.

    Returns plain-text transcript or None on failure.
    The caller is responsible for providing audio in a format Whisper accepts
    (wav, mp3, mp4, m4a, webm, ogg, flac — ACS WAV is fine).
    """
    if not audio_bytes:
        log.warning("transcribe_audio called with empty bytes")
        return None

    if len(audio_bytes) > _WHISPER_MAX_BYTES:
        log.warning(
            "Recording is %.1f MB — exceeds Whisper 25 MB limit; "
            "truncating to first %.1f MB (consider splitting long meetings)",
            len(audio_bytes) / 1_048_576,
            _WHISPER_MAX_BYTES / 1_048_576,
        )
        audio_bytes = audio_bytes[:_WHISPER_MAX_BYTES]

    # Write to a named temp file so the OpenAI SDK can stream it
    suffix = os.path.splitext(filename)[-1] or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)

        with open(tmp_path, "rb") as f:
            result = client.audio.transcriptions.create(
                model=WHISPER_MODEL,
                file=f,
                response_format="text",
            )

        transcript = result if isinstance(result, str) else getattr(result, "text", str(result))
        log.info("Whisper transcription complete (%d chars)", len(transcript))
        return transcript

    except Exception as exc:
        log.error("Whisper transcription failed: %s", exc)
        return None

    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
