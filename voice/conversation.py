"""One live conversation: listens, detects turns, transcribes, asks the LLM, speaks back.

Transport-free: audio comes in through feed() and goes out through a Speaker.
"""

import asyncio
import logging
import math

import numpy as np

from .audio import InputResampler
from .llm import speakable, stream_chat, stream_sentences
from .vad import SileroStream

log = logging.getLogger(__name__)

FRAME_MS = 20
START_MS = 60          # sustained speech needed to count as talking
PREROLL_MS = 300       # audio kept from just before speech starts
MAX_UTTERANCE_S = 30
HANGUP_TAG = "<hangup/>"


def friendly_error(e):
    """What the AI says out loud when its brain fails. Details go to the server log."""
    text = str(e).lower()
    status = getattr(e, "status", None)
    if isinstance(e, asyncio.TimeoutError):
        return "Sorry, my brain is taking too long to answer. Try me again in a minute."
    if status == 429 or "rate limit" in text or "slow down" in text or "too many requests" in text:
        return "My brain is being rate limited right now. Give it a minute and try again."
    if status in (401, 403):
        return "I can't log in to my brain. Check the API key in the settings."
    if status == 404 or "not found" in text or "unknown agent" in text:
        return "My brain's address or model name looks wrong. Check the settings."
    if isinstance(e, (OSError, ConnectionError)) or "cannot connect" in text:
        return "I can't reach my brain right now. Is it running?"
    return "Sorry, I'm having trouble thinking right now."


def rms(pcm):
    if not pcm:
        return 0.0
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    return float(math.sqrt(np.mean(a * a)))


class EnergyVAD:
    """Fallback detector (VAD_BACKEND=energy): anything loud enough above the noise floor counts as speech."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.floor = cfg.vad_min_rms / cfg.vad_ratio

    def __call__(self, pcm):
        level = rms(pcm)
        loud = level > max(self.cfg.vad_min_rms, self.floor * self.cfg.vad_ratio)
        if not loud:
            self.floor = 0.95 * self.floor + 0.05 * level
        return [1.0 if loud else 0.0]


def make_vad(cfg):
    return EnergyVAD(cfg) if cfg.vad_backend == "energy" else SileroStream()


class Conversation:
    def __init__(self, cfg, stt, tts, speaker, call_id, reason=None, on_hangup=None):
        self.cfg, self.stt, self.tts, self.speaker = cfg, stt, tts, speaker
        self.call_id = call_id
        self.reason = reason            # set when the AI placed the call
        self.on_hangup = on_hangup      # async callable(), when the AI ends the call
        self.history = []
        self.transcript = []            # [{"role": "user"|"assistant", "text": ...}]
        self.listening = False          # meeting audio is ignored until someone can be heard
        self.ended = False
        self._task = None               # current response task (thinking/speaking)
        self._untranscribed = b""       # caller audio sent to STT but not transcribed yet
        self._queued = []               # caller turns spoken while the AI was still thinking
        self._frames = asyncio.Queue()
        self._resample = InputResampler()
        self._listen_task = asyncio.ensure_future(self._listen())

    # ---- input ----

    def feed(self, pcm32k):
        """Meeting audio from the bridge (32 kHz 16-bit mono, any chunk size)."""
        if self.ended:
            return
        for frame in self._resample(pcm32k):
            self._frames.put_nowait(frame)

    # ---- call flow ----

    def greet(self):
        """Someone is in the meeting and the bot can hear them: say hello and start listening."""
        self.listening = True
        self._start(self._opener() if self.reason else self._say_fixed(self.cfg.greeting))

    def say(self, text):
        """Say a fixed line (not part of the conversation history), e.g. a permission request."""
        self._start(self._speak_only(text))

    async def close(self):
        if self.ended:
            return
        self.ended = True
        for task in (self._task, self._listen_task):
            if task and not task.done() and task is not asyncio.current_task():
                task.cancel()
        await self.speaker.stop()

    # ---- listening & turn-taking ----

    async def _listen(self):
        vad = make_vad(self.cfg)
        preroll, buf = [], bytearray()
        speech, in_speech, speech_ms, silence_ms = False, False, 0, 0
        preroll_frames = PREROLL_MS // FRAME_MS
        peak_level, peak_prob, frames = 0.0, 0.0, 0
        try:
            while not self.ended:
                pcm = await self._frames.get()
                probs = vad(pcm)
                if probs:  # Silero steps are 32 ms, frames are 20 ms: keep the latest verdict
                    speech = max(probs) >= self.cfg.vad_threshold
                level = rms(pcm)
                # Once a second, log levels (LOG_LEVEL=DEBUG) to help tune VAD settings.
                peak_level, frames = max(peak_level, level), frames + 1
                peak_prob = max([peak_prob] + probs)
                if frames == 50:
                    log.debug("[%s] meeting peak level %.0f, peak speech probability %.2f",
                              self.call_id, peak_level, peak_prob)
                    peak_level, peak_prob, frames = 0.0, 0.0, 0
                if not self.listening:
                    continue
                busy = self._task is not None and not self._task.done()
                talking = self.speaker.playing
                if not busy and not in_speech and self._queued:
                    # Speech that arrived while the AI was thinking gets its own turn next.
                    queued, self._queued = b"".join(self._queued), []
                    self._start(self._respond(queued))
                    continue

                if not in_speech:
                    preroll.append(pcm)
                    del preroll[:-preroll_frames]
                    # While the AI is talking, interrupting takes longer, louder speech.
                    counts = speech and (not talking or level >= self.cfg.vad_min_rms)
                    speech_ms = speech_ms + FRAME_MS if counts else 0
                    if speech_ms >= (self.cfg.barge_in_ms if talking else START_MS):
                        unheard = self._interrupt() if talking else b""
                        in_speech, silence_ms = True, 0
                        buf = bytearray(unheard + b"".join(preroll))
                    continue

                buf += pcm
                silence_ms = 0 if speech else silence_ms + FRAME_MS
                if silence_ms >= self.cfg.silence_ms or len(buf) >= MAX_UTTERANCE_S * 32000:
                    in_speech, speech_ms = False, 0
                    utterance, buf = bytes(buf), bytearray()
                    if self._task is not None and not self._task.done():
                        # The AI is still working on the last turn (a slow model): don't throw that
                        # work away. Answer this right after it.
                        self._queued.append(utterance)
                    else:
                        utterance, self._queued = b"".join(self._queued) + utterance, []
                        self._start(self._respond(utterance))
        except asyncio.CancelledError:
            pass

    def _start(self, coro):
        """Start the next reply. Only one runs at a time: an unfinished one is cancelled."""
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.ensure_future(coro)

    def _interrupt(self):
        """Stop the current reply. Returns audio the caller said that was never transcribed."""
        spoken = self.speaker.playing
        log.info("[%s] caller interrupted%s", self.call_id, "" if spoken else " (before the reply started)")
        self.speaker.clear()
        unheard, self._untranscribed = self._untranscribed, b""
        if self._task and not self._task.done():
            self._task.cancel()
        return unheard

    # ---- responding ----

    def _messages(self):
        system = self.cfg.system_prompt
        if self.reason:
            system += (f"\n\nYou ({self.cfg.ai_name}) started this call with the user. "
                       f"The reason for the call: {self.reason}")
        return [{"role": "system", "content": system}] + self.history

    async def _respond(self, utterance):
        # If the caller keeps talking before this is transcribed, it gets merged into their next turn.
        self._untranscribed = utterance
        text = await self.stt.transcribe(utterance)
        self._untranscribed = b""
        if not text:
            return
        log.info("[%s] user: %s", self.call_id, text)
        self.history.append({"role": "user", "content": text})
        self.transcript.append({"role": "user", "text": text})
        await self._speak_llm()

    async def _opener(self):
        self.history.append({"role": "user", "content": "(The user just joined the call. "
                                                        "Greet them and say why you're calling.)"})
        await self._speak_llm()

    async def _say_fixed(self, text):
        self._remember(text)
        await self._speak(text)
        await self.speaker.wait_idle()

    async def _speak_only(self, text):
        await self._speak(text)
        await self.speaker.wait_idle()

    async def _speak(self, text):
        pcm, rate = await self.tts.synthesize(text)
        self.speaker.play(pcm, rate)

    async def _speak_llm(self):
        spoken, hang_up = [], False
        deltas = stream_chat(self.cfg, self._messages(), user=f"meetbot-{self.call_id}")
        sentences = stream_sentences(speakable(deltas))
        try:
            first = await self._first_sentence(sentences)
            if first is not None:
                pending = [first]
                while pending:
                    sentence = pending.pop()
                    if HANGUP_TAG in sentence:
                        hang_up = True
                        sentence = sentence.replace(HANGUP_TAG, "").strip()
                    if sentence:
                        spoken.append(sentence)
                        await self._speak(sentence)
                    try:
                        pending.append(await sentences.__anext__())
                    except StopAsyncIteration:
                        pass
        except asyncio.CancelledError:
            if spoken:
                self._remember(" ".join(spoken) + " —")
            raise
        except (Exception, asyncio.TimeoutError) as e:
            log.error("[%s] LLM/TTS error: %r", self.call_id, e)
            message = friendly_error(e)
            spoken.append(message)
            await self._speak(message)
        finally:
            await sentences.aclose()
            await deltas.aclose()  # closes the HTTP connection to the LLM
        if not spoken and not hang_up:
            # Everything in the reply was unspeakable (e.g. a raw tool call); don't leave silence.
            log.warning("[%s] reply had nothing speakable", self.call_id)
            spoken.append("Sorry, I got mixed up there. Could you say that again?")
            await self._speak(spoken[0])
        self._remember(" ".join(spoken))
        await self.speaker.wait_idle()
        if hang_up and self.on_hangup:
            await self.on_hangup()

    async def _first_sentence(self, sentences):
        """Wait for the reply to start. Says a filler if it's slow; gives up after LLM_TIMEOUT."""
        first = asyncio.ensure_future(sentences.__anext__())
        try:
            use_filler = 0 < self.cfg.filler_delay < self.cfg.llm_timeout
            done = (await asyncio.wait({first}, timeout=self.cfg.filler_delay))[0] if use_filler else {first}
            if not done:
                fillers = [f.strip() for f in self.cfg.fillers.split("|") if f.strip()]
                if fillers:
                    self._filler_index = (getattr(self, "_filler_index", -1) + 1) % len(fillers)
                    filler = fillers[self._filler_index]
                    log.info("[%s] reply is slow, saying filler: %s", self.call_id, filler)
                    await self._speak(filler)
            remaining = self.cfg.llm_timeout - (self.cfg.filler_delay if use_filler else 0)
            return await asyncio.wait_for(asyncio.shield(first), remaining)
        except StopAsyncIteration:
            return None  # empty reply
        finally:
            if not first.done():
                first.cancel()
                # Let the cancelled read finish before the stream is closed.
                await asyncio.gather(first, return_exceptions=True)

    def _remember(self, text):
        if text:
            self.history.append({"role": "assistant", "content": text})
            self.transcript.append({"role": "assistant", "text": text})
