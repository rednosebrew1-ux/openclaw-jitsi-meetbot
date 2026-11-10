"""Stand-ins for STT, TTS, the LLM, the Jitsi bridge and a push service."""

import asyncio
import json
import time

import aiohttp
import numpy as np
from aiohttp import web

RATE = 32000
CHUNK = RATE // 100 * 2  # 10 ms of 32 kHz 16-bit mono


class FakeSTT:
    def __init__(self, text="hello there"):
        self.text = text
        self.calls = 0

    async def transcribe(self, pcm16k):
        self.calls += 1
        return self.text if pcm16k else ""


class FakeTTS:
    """Speaks a 440 Hz tone, 60 ms per character (capped at max_seconds)."""

    def __init__(self, max_seconds=1.5):
        self.max_seconds = max_seconds
        self.spoken = []

    async def synthesize(self, text):
        self.spoken.append(text)
        rate = 16000
        seconds = min(self.max_seconds, 0.06 * len(text))
        t = np.arange(int(rate * seconds)) / rate
        return (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16).tobytes(), rate


def fake_llm_app(requests, behavior=None):
    """An OpenAI-compatible streaming endpoint. Appends each request body to `requests`.

    `behavior` (a dict you can change between calls): delay (seconds), status (HTTP error),
    reply (fixed reply text).
    """
    behavior = behavior if behavior is not None else {}

    async def chat(request):
        body = await request.json()
        requests.append(body)
        await asyncio.sleep(behavior.get("delay", 0))
        if behavior.get("status"):
            return web.json_response({"error": {"message": "nope"}}, status=behavior["status"])
        last = body["messages"][-1]["content"]
        if behavior.get("reply"):
            reply = behavior["reply"]
        elif "just joined" in last:
            reply = "Hi, it's your assistant. I'm calling because the build finished. Talk soon! <hangup/>"
        else:
            reply = "Got it. Anything else?"
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for word in reply.split(" "):
            chunk = {"choices": [{"delta": {"content": word + " "}}]}
            await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await resp.write(b"data: [DONE]\n\n")
        return resp

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    return app


def fake_notify_app(log):
    async def notify(request):
        log.append({"text": (await request.read()).decode(), "click": request.headers.get("Click")})
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_post("/topic", notify)
    return app


class FakeBridge:
    """Plays the Jitsi bridge: connects to /bridge, records commands, streams room audio.

    The room audio is silence plus any `clips` [(start_s, int16 samples at 32 kHz)],
    paced in real time in 10 ms chunks.
    """

    def __init__(self, base, token):
        self.url = base.replace("http", "ws") + "/bridge"
        self.token = token
        self.commands = []
        self.loud_frames = 0
        self.audio_frames = 0
        self.clips = []
        self.ws = None
        self._session = None
        self._tasks = []
        self.t0 = None

    async def connect(self):
        self._session = aiohttp.ClientSession()
        self.ws = await self._session.ws_connect(self.url, headers={"Authorization": f"Bearer {self.token}"})
        await self.ws.send_json({"type": "hello"})
        self._tasks.append(asyncio.ensure_future(self._read()))
        return self

    def start_audio(self, clips=()):
        """Start streaming meeting audio; clip times are relative to now."""
        self.clips = list(clips)
        self.t0 = time.time()
        self._tasks.append(asyncio.ensure_future(self._stream()))

    async def send(self, **event):
        await self.ws.send_json(event)

    async def join_meeting(self, others=1, recording=True):
        """Report what the bridge reports once it's in the room."""
        await self.send(type="status", status="joining")
        await self.send(type="status", status="in_meeting")
        await self.send(type="recording", allowed=recording)
        await self.send(type="mic_ready")
        await self.send(type="participants", others=others)

    def commands_of(self, kind):
        return [c for c in self.commands if c.get("type") == kind]

    async def _read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                self.commands.append(json.loads(msg.data))
            elif msg.type == aiohttp.WSMsgType.BINARY:
                self.audio_frames += 1
                if np.abs(np.frombuffer(msg.data, dtype=np.int16)).max() > 1000:
                    self.loud_frames += 1

    async def _stream(self):
        sent = 0
        try:
            while not self.ws.closed:
                now = sent * 0.01
                chunk = np.zeros(CHUNK // 2, dtype=np.int16)
                for start, samples in self.clips:
                    offset = int((now - start) * RATE)
                    if 0 <= offset < len(samples):
                        piece = samples[offset: offset + len(chunk)]
                        chunk[: len(piece)] = piece
                await self.ws.send_bytes(chunk.tobytes())
                sent += 1
                wait = self.t0 + sent * 0.01 - time.time()
                if wait > 0:
                    await asyncio.sleep(wait)
        except (ConnectionError, RuntimeError):
            pass

    async def close(self):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.ws is not None:
            await self.ws.close()
        if self._session is not None:
            await self._session.close()
