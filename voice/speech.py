"""Speech-to-text and text-to-speech backends.

Every backend works on 16-bit mono PCM:
  STT: transcribe(pcm16k: bytes) -> str          (16 kHz input)
  TTS: synthesize(text: str) -> (pcm: bytes, rate: int)
"""

import asyncio
import io
import logging
import os
import urllib.request
import wave

import aiohttp
import numpy as np

log = logging.getLogger(__name__)

PIPER_VOICES_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def pcm_to_wav(pcm, rate):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def wav_to_pcm(data):
    """Decode a WAV file to (mono 16-bit pcm, rate)."""
    with wave.open(io.BytesIO(data), "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"expected 16-bit WAV, got {width * 8}-bit")
    if channels > 1:
        frames = np.frombuffer(frames, dtype=np.int16).reshape(-1, channels).mean(axis=1).astype(np.int16).tobytes()
    return frames, rate


# ---- speech-to-text ---------------------------------------------------------

class FasterWhisperSTT:
    def __init__(self, cfg):
        from faster_whisper import WhisperModel

        log.info("Loading Whisper model %s (first run downloads it)...", cfg.whisper_model)
        self.model = WhisperModel(
            cfg.whisper_model,
            device=cfg.whisper_device,
            compute_type="int8",
            download_root=os.path.join(cfg.data_dir, "whisper"),
        )

    def _run(self, pcm16k):
        audio = np.frombuffer(pcm16k, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = self.model.transcribe(audio, beam_size=1, vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()

    async def transcribe(self, pcm16k):
        return await asyncio.to_thread(self._run, pcm16k)


class OpenAISTT:
    def __init__(self, cfg):
        self.cfg = cfg

    async def transcribe(self, pcm16k):
        form = aiohttp.FormData()
        form.add_field("model", self.cfg.stt_model)
        form.add_field("file", pcm_to_wav(pcm16k, 16000), filename="speech.wav", content_type="audio/wav")
        headers = {"Authorization": f"Bearer {self.cfg.stt_api_key}"} if self.cfg.stt_api_key else {}
        async with aiohttp.ClientSession() as s:
            async with s.post(self.cfg.stt_url, data=form, headers=headers) as r:
                r.raise_for_status()
                return (await r.json()).get("text", "").strip()


# ---- text-to-speech ---------------------------------------------------------

def _piper_voice_path(cfg):
    """Accept a path to an .onnx file, or a voice name like en_US-lessac-medium (downloaded once)."""
    if cfg.piper_voice.endswith(".onnx"):
        return cfg.piper_voice
    name = cfg.piper_voice
    lang, speaker, quality = name.split("-", 2)
    voice_dir = os.path.join(cfg.data_dir, "piper")
    os.makedirs(voice_dir, exist_ok=True)
    path = os.path.join(voice_dir, f"{name}.onnx")
    for suffix in (".onnx", ".onnx.json"):
        target = path.replace(".onnx", suffix)
        if not os.path.exists(target):
            url = f"{PIPER_VOICES_URL}/{lang.split('_')[0]}/{lang}/{speaker}/{quality}/{name}{suffix}"
            log.info("Downloading Piper voice %s", url)
            urllib.request.urlretrieve(url, target + ".part")
            os.replace(target + ".part", target)
    return path


class PiperTTS:
    def __init__(self, cfg):
        from piper import PiperVoice

        self.voice = PiperVoice.load(_piper_voice_path(cfg))

    def _run(self, text):
        chunks = list(self.voice.synthesize(text))
        if not chunks:
            return b"", 22050
        return b"".join(c.audio_int16_bytes for c in chunks), chunks[0].sample_rate

    async def synthesize(self, text):
        return await asyncio.to_thread(self._run, text)


class OpenAITTS:
    def __init__(self, cfg):
        self.cfg = cfg

    async def synthesize(self, text):
        body = {"model": self.cfg.tts_model, "voice": self.cfg.tts_voice, "input": text, "response_format": "wav"}
        headers = {"Authorization": f"Bearer {self.cfg.tts_api_key}"} if self.cfg.tts_api_key else {}
        async with aiohttp.ClientSession() as s:
            async with s.post(self.cfg.tts_url, json=body, headers=headers) as r:
                r.raise_for_status()
                return wav_to_pcm(await r.read())


def make_stt(cfg):
    backends = {"faster-whisper": FasterWhisperSTT, "openai": OpenAISTT}
    return backends[cfg.stt_backend](cfg)


def make_tts(cfg):
    backends = {"piper": PiperTTS, "openai": OpenAITTS}
    return backends[cfg.tts_backend](cfg)
