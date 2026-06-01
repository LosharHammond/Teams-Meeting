"""
Azure Communication Services (ACS) Bot
=======================================
Joins Teams meetings automatically and records them so recaps work even
when participants don't start recording manually.

Flow:
  1. cron.py detects a meeting that just started → join_and_record()
  2. ACS manages the call on Azure's side; our server is free between events
  3. ACS fires webhook events to /api/acs-callback:
       CallConnected        → bot is in call → start_recording()
       RecordingStateChanged → recording confirmed active → DB: BOT_RECORDING
       RecordingFileStatusUpdated → recording ready → DB stores blob URL
       CallDisconnected     → meeting ended (already handled via recording URL)
  4. pipeline.py detects TRANSCRIPT_PENDING with a recording_url → transcribe_recording()

Requires (all optional — bot is silently disabled when ACS_CONNECTION_STRING is empty):
  ACS_CONNECTION_STRING            from Azure Communication Services resource
  AZURE_STORAGE_CONNECTION_STRING  from Azure Storage account
  ACS_CALLBACK_BASE_URL            your Railway app public URL
  ENABLE_BOT_RECORDING=true
"""
import logging
from typing import Optional

from core.config import (
    ACS_CONNECTION_STRING,
    ACS_CALLBACK_BASE_URL,
    AZURE_STORAGE_CONN_STR,
    BOT_DISPLAY_NAME,
    RECORDINGS_CONTAINER,
    ENABLE_BOT_RECORDING,
)

log = logging.getLogger("summarizer.bot")


def _client():
    from azure.communication.callautomation import CallAutomationClient
    return CallAutomationClient.from_connection_string(ACS_CONNECTION_STRING)


def join_and_record(join_url: str, event_hash: str) -> Optional[str]:
    """
    Dispatch the ACS bot to join a Teams meeting.

    Returns the call_connection_id (stored in DB so we can correlate
    subsequent webhook events back to this meeting), or None on failure.

    The bot appears in the participant list as BOT_DISPLAY_NAME so that
    all participants are aware the meeting is being recorded.
    """
    if not ENABLE_BOT_RECORDING:
        return None

    try:
        from azure.communication.callautomation import TeamsMeetingLinkLocator
        from azure.communication.callautomation.models import CallInvite

        client = _client()
        callback_url = f"{ACS_CALLBACK_BASE_URL}/api/acs-callback?event_hash={event_hash}"

        result = client.join_call(
            meeting_locator=TeamsMeetingLinkLocator(meeting_link=join_url),
            callback_url=callback_url,
            operation_context=event_hash,
        )
        log.info(
            "Bot joining meeting (event_hash=%s), call_connection_id=%s",
            event_hash, result.call_connection_id,
        )
        return result.call_connection_id

    except Exception as exc:
        log.warning("Bot failed to join meeting (event_hash=%s): %s", event_hash, exc)
        return None


def start_recording(server_call_id: str, event_hash: str) -> Optional[str]:
    """
    Start ACS cloud recording once the bot is connected (called from
    the CallConnected webhook event).

    Records audio-only in WAV format (cheaper + sufficient for transcription).
    Returns the recording_id or None on failure.
    """
    if not ENABLE_BOT_RECORDING:
        return None

    try:
        from azure.communication.callautomation import ServerCallLocator
        from azure.communication.callautomation.models import (
            StartRecordingOptions,
            RecordingContent,
            RecordingChannel,
            RecordingFormat,
        )

        client = _client()
        callback_url = f"{ACS_CALLBACK_BASE_URL}/api/acs-callback?event_hash={event_hash}"

        result = client.start_recording(
            StartRecordingOptions(
                call_locator=ServerCallLocator(server_call_id=server_call_id),
                recording_state_callback_url=callback_url,
                # Audio-only WAV — smaller files, faster Whisper processing
                recording_content_type=RecordingContent.AUDIO,
                recording_channel_type=RecordingChannel.MIXED,
                recording_format_type=RecordingFormat.WAV,
            )
        )
        log.info(
            "Recording started (server_call_id=%s), recording_id=%s",
            server_call_id, result.recording_id,
        )
        return result.recording_id

    except Exception as exc:
        log.warning("Failed to start recording (server_call_id=%s): %s", server_call_id, exc)
        return None


def download_recording(recording_url: str) -> Optional[bytes]:
    """
    Download a finished ACS recording from Azure Blob Storage.
    Returns the raw audio bytes or None on failure.

    recording_url format:
      https://<account>.blob.core.windows.net/<container>/<blob_path>
    """
    try:
        from azure.storage.blob import BlobServiceClient

        # Parse blob URL → container + blob name
        # Strip protocol and split on first slash after the hostname
        path = recording_url.split(".blob.core.windows.net/", 1)[-1]
        container, _, blob_name = path.partition("/")

        blob_service = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONN_STR)
        blob_client  = blob_service.get_blob_client(
            container=container or RECORDINGS_CONTAINER,
            blob=blob_name,
        )
        data = blob_client.download_blob().readall()
        log.info("Downloaded recording (%d bytes)", len(data))
        return data

    except Exception as exc:
        log.warning("Failed to download recording from %s: %s", recording_url, exc)
        return None
