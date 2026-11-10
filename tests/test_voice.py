"""Voice server tests: a fake bridge plays the Jitsi room, with fake STT/TTS/LLM.

Run: python -m pytest tests
"""

import asyncio
import os
import time
import wave

import aiohttp
import numpy as np
import pytest
from aiohttp import web

from tests.fakes import FakeBridge, FakeSTT, FakeTTS, fake_llm_app, fake_notify_app
from voice.app import Call, MeetBotApp
from voice.audio import resample_pcm
from voice.config import Settings
from voice.rooms import jitsi_room

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SPEECH_WAV = os.path.join(os.path.dirname(__file__), "fixtures", "speech.wav")
JITSI = "https://meet.example.com"
ALWAYS_ON = "https://meet.example.com/assistantroom"

Call.GREET_DELAY = 0.1


def load_speech():
    """A real spoken sentence (espeak-ng), as 32 kHz 16-bit mono samples."""
    with wave.open(SPEECH_WAV) as w:
        pcm = resample_pcm(w.readframes(w.getnframes()), w.getframerate(), 32000)
    return np.frombuffer(pcm, dtype=np.int16)


async def serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


class Env:
    """A running voice server plus its fakes."""

    async def start(self, notify=False, tts_max_seconds=1.5, llm=None, **overrides):
        self.runners = []
        self.llm_requests, self.notify_log = [], []
        runner, llm_base = await serve(fake_llm_app(self.llm_requests, llm))
        self.runners.append(runner)
        cfg = Settings()
        cfg.api_token = TOKEN
        cfg.llm_url = llm_base + "/v1/chat/completions"
        cfg.llm_api_key = ""
        cfg.filler_delay = 0
        cfg.join_timeout = 20
        cfg.jitsi_url = JITSI
        cfg.always_on_meeting_url = ""
        cfg.notify_url = ""
        if notify:
            runner, notify_base = await serve(fake_notify_app(self.notify_log))
            self.runners.append(runner)
            cfg.notify_url = notify_base + "/topic"
        for key, value in overrides.items():
            setattr(cfg, key, value)
        self.stt, self.tts = FakeSTT(), FakeTTS(max_seconds=tts_max_seconds)
        self.app = MeetBotApp(cfg, self.stt, self.tts)
        runner, self.base = await serve(self.app.make_app())
        self.runners.append(runner)
        self.http = aiohttp.ClientSession()
        self.bridges = []
        return self

    async def bridge(self):
        b = await FakeBridge(self.base, TOKEN).connect()
        self.bridges.append(b)
        assert await wait_for(lambda: self.app.bridge is not None)
        return b

    async def post(self, path, body=None, headers=AUTH):
        async with self.http.post(self.base + path, json=body or {}, headers=headers) as r:
            return r.status, await r.json(content_type=None)

    async def get(self, path):
        async with self.http.get(self.base + path, headers=AUTH) as r:
            return r.status, await r.json(content_type=None)

    async def stop(self):
        for b in self.bridges:
            await b.close()
        await self.http.close()
        for r in reversed(self.runners):
            await r.cleanup()


async def wait_for(predicate, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 60))


# ---- the AI calls you ----

async def _ai_calls_you():
    env = await Env().start(notify=True)
    try:
        status, _ = await env.post("/api/call", {"reason": "The build finished."}, headers={})
        assert status == 401
        status, body = await env.post("/api/call", {"reason": "The build finished."})
        assert status == 503 and "bridge" in body["error"]

        bridge = await env.bridge()
        status, call = await env.post("/api/call", {"reason": "The build finished."})
        assert status == 200, call
        # A fresh, hard-to-guess room on your server.
        assert call["join_url"].startswith(JITSI + "/assistant") and len(call["join_url"]) > len(JITSI) + 15
        assert await wait_for(lambda: bridge.commands_of("join"))
        join = bridge.commands_of("join")[0]
        assert join == {"type": "join", "url": call["join_url"], "display_name": env.app.cfg.ai_name}

        # You get the link on your phone.
        assert await wait_for(lambda: env.notify_log)
        assert env.notify_log[0]["click"] == call["join_url"] and call["join_url"] in env.notify_log[0]["text"]

        # A second call while this one is going is refused.
        status, body = await env.post("/api/call", {"reason": "again"})
        assert status == 409

        # The bot is in the room, alone: it waits quietly.
        bridge.start_audio()
        await bridge.join_meeting(others=0)
        await asyncio.sleep(0.5)
        assert env.llm_requests == [] and bridge.loud_frames == 0
        _, info = await env.get(f"/api/calls/{call['id']}")
        assert info["status"] == "waiting"

        # You join: it says why it called, and hangs up as the reply asks.
        await bridge.send(type="participants", others=1)
        assert await wait_for(lambda: env.llm_requests)
        assert "The build finished." in env.llm_requests[0]["messages"][0]["content"]
        assert await wait_for(lambda: bridge.loud_frames > 20)
        assert await wait_for(lambda: bridge.commands_of("leave"))

        _, done = await env.get(f"/api/calls/{call['id']}?wait=1&timeout=5")
        assert done["status"] == "completed"
        assert done["transcript"][0]["role"] == "assistant"
        assert "build finished" in done["transcript"][0]["text"]
        assert "<hangup/>" not in done["transcript"][0]["text"]
        assert env.app.call is None
    finally:
        await env.stop()


def test_ai_calls_you():
    run(_ai_calls_you())


async def _no_room_to_use():
    env = await Env().start(jitsi_url="")
    try:
        await env.bridge()
        status, body = await env.post("/api/call", {"reason": "x"})
        assert status == 400 and "JITSI_URL" in body["error"]
        status, body = await env.post("/api/join", {"link": "AssistantRoom"})
        assert status == 400 and "JITSI_URL" in body["error"]
        status, body = await env.post("/api/join", {})
        assert status == 400 and "ALWAYS_ON_MEETING_URL" in body["error"]
    finally:
        await env.stop()


def test_no_room_to_use():
    run(_no_room_to_use())


# ---- you call it ----

async def _you_call_it():
    env = await Env().start()
    try:
        bridge = await env.bridge()
        status, body = await env.post("/api/join", {"link": "not a room/really"})
        assert status == 400
        # A room name on your server works as well as a full link.
        status, call = await env.post("/api/join", {"link": "MyRoom"})
        assert status == 200, call
        assert call["join_url"] == JITSI + "/myroom"
        assert await wait_for(lambda: bridge.commands_of("join"))
        assert bridge.commands_of("join")[0]["url"] == JITSI + "/myroom"

        # Say something 2 s in, after the greeting.
        bridge.start_audio(clips=[(2.0, load_speech())])
        await bridge.join_meeting(others=1)
        assert await wait_for(lambda: env.tts.spoken and env.tts.spoken[0] == env.app.cfg.greeting)
        assert await wait_for(lambda: env.llm_requests, timeout=15)
        assert env.llm_requests[0]["messages"][-1] == {"role": "user", "content": "hello there"}
        assert env.llm_requests[0]["model"] == env.app.cfg.llm_model
        assert await wait_for(lambda: "Got it." in env.tts.spoken)

        # Everyone else leaves: the bot leaves too.
        await bridge.send(type="participants", others=0)
        assert await wait_for(lambda: bridge.commands_of("leave"))
        _, done = await env.get(f"/api/calls/{call['id']}?wait=1&timeout=5")
        assert done["status"] == "completed"
        roles = [t["role"] for t in done["transcript"]]
        assert roles[:3] == ["assistant", "user", "assistant"], done["transcript"]
    finally:
        await env.stop()


def test_you_call_it():
    run(_you_call_it())


async def _barge_in():
    # A long reply, interrupted by real speech while it plays.
    env = await Env().start(tts_max_seconds=6, llm={"reply": "This is a very long answer. " * 6})
    try:
        bridge = await env.bridge()
        await env.post("/api/join", {"link": "room1"})
        bridge.start_audio(clips=[(1.0, load_speech()), (6.0, load_speech())])
        await bridge.join_meeting()
        assert await wait_for(lambda: len(env.llm_requests) >= 1, timeout=15)
        replied_at = time.time()
        assert await wait_for(lambda: len(env.llm_requests) >= 2, timeout=15), "the interruption wasn't heard"
        # The reply is about 10 s of audio; the second turn starts long before it could have finished.
        assert time.time() - replied_at < 7
        assert env.llm_requests[1]["messages"][-1] == {"role": "user", "content": "hello there"}
    finally:
        await env.stop()


def test_barge_in():
    run(_barge_in())


async def _waits_until_it_can_hear():
    env = await Env().start()
    try:
        bridge = await env.bridge()
        await env.post("/api/join", {"link": "room1"})
        await bridge.join_meeting(recording=False)
        await asyncio.sleep(0.5)
        assert env.tts.spoken == [] and env.app.call.status == "waiting"
        await bridge.send(type="recording", allowed=True)
        assert await wait_for(lambda: env.app.cfg.greeting in env.tts.spoken)
        assert env.app.call.status == "in_call"
    finally:
        await env.stop()


def test_waits_until_it_can_hear():
    run(_waits_until_it_can_hear())


async def _nobody_comes():
    env = await Env().start(join_timeout=1)
    try:
        bridge = await env.bridge()
        _, call = await env.post("/api/join", {"link": "room1"})
        await bridge.join_meeting(others=0)
        assert await wait_for(lambda: bridge.commands_of("leave"), timeout=5)
        _, done = await env.get(f"/api/calls/{call['id']}?wait=1&timeout=5")
        assert done["status"] == "missed"
    finally:
        await env.stop()


def test_nobody_comes():
    run(_nobody_comes())


async def _join_fails_and_bridge_drops():
    env = await Env().start()
    try:
        bridge = await env.bridge()
        _, call = await env.post("/api/join", {"link": "room1"})
        await bridge.send(type="ended", failed=True, reason="couldn't open the room: net::ERR_CONNECTION_REFUSED")
        _, done = await env.get(f"/api/calls/{call['id']}?wait=1&timeout=5")
        assert done["status"] == "failed" and "ERR_CONNECTION_REFUSED" in done["error"]

        # The bridge dropping mid-call ends the call; a new bridge can connect.
        _, call = await env.post("/api/join", {"link": "room1"})
        await bridge.close()
        _, done = await env.get(f"/api/calls/{call['id']}?wait=1&timeout=5")
        assert done["status"] == "failed" and "disconnected" in done["error"]
        await env.bridge()
    finally:
        await env.stop()


def test_join_fails_and_bridge_drops():
    run(_join_fails_and_bridge_drops())


async def _hang_up_from_api():
    env = await Env().start()
    try:
        bridge = await env.bridge()
        _, call = await env.post("/api/join", {"link": "room1"})
        status, info = await env.post(f"/api/calls/{call['id']}/hangup")
        assert status == 200 and info["status"] == "missed"
        assert await wait_for(lambda: bridge.commands_of("leave"))
        status, _ = await env.post("/api/calls/nope/hangup")
        assert status == 404
    finally:
        await env.stop()


def test_hang_up_from_api():
    run(_hang_up_from_api())


async def _bridge_needs_token():
    env = await Env().start()
    try:
        bad = FakeBridge(env.base, "wrong")
        with pytest.raises(aiohttp.WSServerHandshakeError):
            await bad.connect()
        await bad.close()
    finally:
        await env.stop()


def test_bridge_needs_token():
    run(_bridge_needs_token())


# ---- always-on room ----

async def _always_on():
    env = await Env().start(notify=True, always_on_meeting_url="https://Meet.Example.com/AssistantRoom")
    try:
        # As soon as the bridge connects, the bot goes and waits in the always-on room.
        bridge = await env.bridge()
        assert await wait_for(lambda: bridge.commands_of("join"))
        assert bridge.commands_of("join")[0]["url"] == ALWAYS_ON
        bridge.start_audio()
        await bridge.join_meeting(others=0)
        await asyncio.sleep(0.3)
        _, status = await env.get("/api/status")
        assert status["always_on"] == {"join_url": ALWAYS_ON, "bot_in_room": True}
        assert status["call"] is None and env.tts.spoken == []

        # You drop in: it greets you. You leave: it stays.
        await bridge.send(type="participants", others=1)
        assert await wait_for(lambda: env.tts.spoken == [env.app.cfg.greeting])
        first = env.app.call
        assert first.kind == "standby" and first.info()["always_on"] is True
        await bridge.send(type="participants", others=0)
        assert await wait_for(lambda: first.finished)
        assert first.status == "completed" and env.app.call is None
        await asyncio.sleep(0.3)
        assert not bridge.commands_of("leave")

        # You drop in again later: a fresh conversation.
        await bridge.send(type="participants", others=1)
        assert await wait_for(lambda: env.tts.spoken.count(env.app.cfg.greeting) == 2)
        second = env.app.call
        assert second is not first and second.conversation.history[0]["content"] == env.app.cfg.greeting
        await bridge.send(type="participants", others=0)
        assert await wait_for(lambda: second.finished)

        # "Call me" (and "join" without a link) send the always-on link instead of making a room.
        status, call = await env.post("/api/call", {"reason": "The build finished."})
        assert status == 200 and call["join_url"] == ALWAYS_ON and call["status"] == "waiting"
        assert len(bridge.commands_of("join")) == 1
        assert await wait_for(lambda: env.notify_log and env.notify_log[0]["click"] == ALWAYS_ON)
        await bridge.send(type="participants", others=1)
        assert await wait_for(lambda: env.llm_requests)
        assert "The build finished." in env.llm_requests[0]["messages"][0]["content"]
        # The reply ends with <hangup/>: the conversation ends but the bot stays in the room,
        # and it doesn't start talking again while you're still there.
        _, done = await env.get(f"/api/calls/{call['id']}?wait=1&timeout=10")
        assert done["status"] == "completed" and "build finished" in done["transcript"][0]["text"]
        await asyncio.sleep(0.5)
        assert env.app.call is None and not bridge.commands_of("leave")
        await bridge.send(type="participants", others=0)
        await bridge.send(type="participants", others=1)
        assert await wait_for(lambda: env.app.call is not None and env.app.call.kind == "standby")
    finally:
        await env.stop()


def test_always_on():
    run(_always_on())


async def _always_on_rejoins():
    env = await Env().start(always_on_meeting_url=ALWAYS_ON)
    try:
        bridge = await env.bridge()
        assert await wait_for(lambda: bridge.commands_of("join"))
        await bridge.join_meeting(others=0)
        # The room closes under the bot (the Jitsi server restarted, say); the bridge says it's free...
        await bridge.send(type="ended", failed=False, reason="the meeting ended")
        await bridge.send(type="hello")
        # ...and the bot goes straight back.
        assert await wait_for(lambda: len(bridge.commands_of("join")) == 2)
        assert bridge.commands_of("join")[1]["url"] == ALWAYS_ON
        assert env.app.standby_failures == 0

        # Repeated failures back off instead of hammering the server.
        await bridge.send(type="ended", failed=True, reason="couldn't open the room")
        assert await wait_for(lambda: env.app.standby_failures == 1)
    finally:
        await env.stop()


def test_always_on_rejoins():
    run(_always_on_rejoins())


async def _always_on_steps_out_for_another_room():
    env = await Env().start(always_on_meeting_url=ALWAYS_ON)
    try:
        bridge = await env.bridge()
        assert await wait_for(lambda: bridge.commands_of("join"))
        await bridge.join_meeting(others=0)

        # Asked to join a different room: it leaves the always-on room first...
        status, call = await env.post("/api/join", {"link": "https://other.example.com/Standup"})
        assert status == 200 and call["status"] == "joining" and call["always_on"] is False
        assert await wait_for(lambda: bridge.commands_of("leave"))
        assert len(bridge.commands_of("join")) == 1
        await bridge.send(type="ended", failed=False, reason="left")
        await bridge.send(type="hello")

        # ...joins the other room once the bridge is free...
        assert await wait_for(lambda: len(bridge.commands_of("join")) == 2)
        assert bridge.commands_of("join")[1]["url"] == "https://other.example.com/standup"
        await bridge.join_meeting(others=1)
        assert await wait_for(lambda: env.app.cfg.greeting in env.tts.spoken)
        await bridge.send(type="participants", others=0)
        assert await wait_for(lambda: len(bridge.commands_of("leave")) == 2)
        _, done = await env.get(f"/api/calls/{call['id']}")
        assert done["status"] == "completed"
        await bridge.send(type="ended", failed=False, reason="left")
        await bridge.send(type="hello")

        # ...and goes back to the always-on room afterwards.
        assert await wait_for(lambda: len(bridge.commands_of("join")) == 3)
        assert bridge.commands_of("join")[2]["url"] == ALWAYS_ON
    finally:
        await env.stop()


def test_always_on_steps_out_for_another_room():
    run(_always_on_steps_out_for_another_room())


# ---- room links ----

@pytest.mark.parametrize("link,expected", [
    ("https://meet.example.com/MyRoom", "https://meet.example.com/myroom"),
    ("https://Meet.Example.com:8443/MyRoom#config.x=1", "https://meet.example.com:8443/myroom"),
    ("https://meet.example.com/MyRoom?jwt=abc", "https://meet.example.com/myroom?jwt=abc"),
    ("MyRoom", "https://base.example.com/myroom"),
])
def test_jitsi_room(link, expected):
    assert jitsi_room(link, "https://base.example.com/") == expected


@pytest.mark.parametrize("link", ["", "https://meet.example.com/", "ftp://x/y", "two words", "a/b"])
def test_jitsi_room_rejects(link):
    with pytest.raises(ValueError):
        jitsi_room(link, "https://base.example.com")


def test_room_name_needs_server():
    with pytest.raises(ValueError, match="JITSI_URL"):
        jitsi_room("MyRoom", "")
