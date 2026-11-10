"""Settings, read once from environment variables. See .env.example for docs."""

import os
import re
from dataclasses import dataclass, field


def load_dotenv(path=".env"):
    """Load KEY=VALUE lines from .env without overriding real environment variables.

    If a key appears more than once, the last line wins (like other dotenv tools).
    """
    if not os.path.exists(path):
        return
    values = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0]  # strip "  # comment"
            values[key.strip()] = value
    for key, value in values.items():
        os.environ.setdefault(key, value)


def _env(name, default=""):
    return os.environ.get(name, default)


def _float(name, default):
    return float(os.environ.get(name, default))


@dataclass
class Settings:
    host: str = field(default_factory=lambda: _env("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_env("PORT", "8090")))
    data_dir: str = field(default_factory=lambda: _env("DATA_DIR", "./data"))

    # Required: protects the API (and the bridge connection) from anyone else on your network.
    api_token: str = field(default_factory=lambda: _env("API_TOKEN"))

    # The brain: any OpenAI-compatible chat completions endpoint. Point it at the OpenClaw Gateway
    # (model "openclaw:<agentId>") so the call goes through your agent and everything it can reach.
    llm_url: str = field(default_factory=lambda: _env("LLM_URL", "http://127.0.0.1:18789/v1/chat/completions"))
    llm_api_key: str = field(default_factory=lambda: _env("LLM_API_KEY"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "openclaw:main"))
    system_prompt: str = field(default_factory=lambda: _env(
        "SYSTEM_PROMPT",
        "You are a friendly voice assistant talking with someone on a live Zoom call. "
        "Speak naturally in short sentences, one to three at a time. Never use lists, "
        "markdown, emojis, or URLs, since everything you say is read aloud. "
        "When the conversation is clearly over and you've said goodbye, end your "
        "reply with <hangup/>.",
    ))

    # When the LLM is slow: say a filler after FILLER_DELAY seconds (0 = never), give up after LLM_TIMEOUT.
    filler_delay: float = field(default_factory=lambda: _float("FILLER_DELAY", "2.5"))
    fillers: str = field(default_factory=lambda: _env("FILLERS", "One sec.|Hmm, let me think.|Give me a moment."))
    llm_timeout: float = field(default_factory=lambda: _float("LLM_TIMEOUT", "30"))

    # Speech-to-text: "faster-whisper" (local) or "openai" (any compatible /v1/audio/transcriptions).
    stt_backend: str = field(default_factory=lambda: _env("STT_BACKEND", "faster-whisper"))
    whisper_model: str = field(default_factory=lambda: _env("WHISPER_MODEL", "base.en"))
    whisper_device: str = field(default_factory=lambda: _env("WHISPER_DEVICE", "cpu"))
    stt_url: str = field(default_factory=lambda: _env("STT_URL", "https://api.openai.com/v1/audio/transcriptions"))
    stt_api_key: str = field(default_factory=lambda: _env("STT_API_KEY"))
    stt_model: str = field(default_factory=lambda: _env("STT_MODEL", "whisper-1"))

    # Text-to-speech: "piper" (local) or "openai" (any compatible /v1/audio/speech).
    tts_backend: str = field(default_factory=lambda: _env("TTS_BACKEND", "piper"))
    piper_voice: str = field(default_factory=lambda: _env("PIPER_VOICE", "en_US-lessac-medium"))
    tts_url: str = field(default_factory=lambda: _env("TTS_URL", "https://api.openai.com/v1/audio/speech"))
    tts_api_key: str = field(default_factory=lambda: _env("TTS_API_KEY"))
    tts_model: str = field(default_factory=lambda: _env("TTS_MODEL", "gpt-4o-mini-tts"))
    tts_voice: str = field(default_factory=lambda: _env("TTS_VOICE", "alloy"))

    # Calls
    ai_name: str = field(default_factory=lambda: _env("AI_NAME", "Assistant"))
    greeting: str = field(default_factory=lambda: _env("GREETING", "Hey, it's your assistant. What's up?"))
    # How long the bot waits in a meeting for you to show up before giving up.
    join_timeout: float = field(default_factory=lambda: _float("JOIN_TIMEOUT", "180"))
    # Said when the bot can't hear the meeting yet (the host hasn't allowed it to record).
    permission_prompt: str = field(default_factory=lambda: _env(
        "PERMISSION_PROMPT",
        "Hi, I'm here, but I can't hear you yet. Please allow me to record so I can listen.",
    ))

    # Zoom: the meeting the bot uses when there's no Zoom API access, or when you say "join my meeting".
    # Paste your personal meeting link (it includes the passcode).
    personal_meeting_url: str = field(default_factory=lambda: _env("PERSONAL_MEETING_URL"))
    # Always-on: the bot stays in this meeting all the time; join it whenever you want to talk.
    # "Call me" sends you this link instead of making a new meeting.
    always_on_meeting_url: str = field(default_factory=lambda: _env("ALWAYS_ON_MEETING_URL"))
    # Optional Server-to-Server OAuth app: lets the bot create a fresh meeting for every call and
    # listen without asking the host for recording permission.
    zoom_account_id: str = field(default_factory=lambda: _env("ZOOM_ACCOUNT_ID"))
    zoom_client_id: str = field(default_factory=lambda: _env("ZOOM_CLIENT_ID"))
    zoom_client_secret: str = field(default_factory=lambda: _env("ZOOM_CLIENT_SECRET"))
    zoom_api_url: str = field(default_factory=lambda: _env("ZOOM_API_URL", "https://api.zoom.us/v2"))
    zoom_oauth_url: str = field(default_factory=lambda: _env("ZOOM_OAUTH_URL", "https://zoom.us/oauth/token"))
    # "On behalf of" token, needed to join meetings hosted outside your Zoom account (Zoom has
    # required it since March 2026). It expires after about two hours; you can also pass one per call.
    zoom_obf_token: str = field(default_factory=lambda: _env("ZOOM_OBF_TOKEN"))
    # Optional webhook: join automatically when one of these meetings starts.
    zoom_webhook_secret: str = field(default_factory=lambda: _env("ZOOM_WEBHOOK_SECRET"))
    auto_join_meetings: str = field(default_factory=lambda: _env("AUTO_JOIN_MEETINGS"))

    # Optional: POST the join link here when the AI calls you, e.g. an ntfy.sh topic URL
    # for a push notification on your phone.
    notify_url: str = field(default_factory=lambda: _env("NOTIFY_URL"))

    # Voice activity detection: "silero" (speech model bundled with faster-whisper) or "energy" (loudness).
    vad_backend: str = field(default_factory=lambda: _env("VAD_BACKEND", "silero"))
    vad_threshold: float = field(default_factory=lambda: _float("VAD_THRESHOLD", "0.5"))
    # Minimum level to interrupt the AI (and, for the energy backend, to count as speech).
    vad_min_rms: float = field(default_factory=lambda: _float("VAD_MIN_RMS", "150"))
    vad_ratio: float = field(default_factory=lambda: _float("VAD_RATIO", "3"))
    silence_ms: float = field(default_factory=lambda: _float("SILENCE_MS", "700"))
    barge_in_ms: float = field(default_factory=lambda: _float("BARGE_IN_MS", "300"))

    @property
    def zoom_api_enabled(self):
        return bool(self.zoom_account_id and self.zoom_client_id and self.zoom_client_secret)

    def auto_join_list(self):
        """AUTO_JOIN_MEETINGS as {meeting_id: passcode or ""}. Entries are "id" or "id:passcode"."""
        out = {}
        for entry in filter(None, (e.strip() for e in self.auto_join_meetings.split(","))):
            number, _, passcode = entry.partition(":")
            number = re.sub(r"\D", "", number)
            if number:
                out[number] = passcode.strip()
        return out


load_dotenv()
settings = Settings()
