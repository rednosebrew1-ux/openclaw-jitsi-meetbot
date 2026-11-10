"""Voice server tests: a fake bridge plays Zoom, with fake STT/TTS/LLM and a fake Zoom API.

Run: python -m pytest tests
"""

import asyncio
import hashlib
import hmac
import json
import os
import time
import wave

import aiohttp
import numpy as np
import pytest
from aiohttp import web

from tests.fakes import FakeBridge, FakeSTT, FakeTTS, fake_llm_app, fake_notify_app, fake_zoom_api_app
from voice.app import Call, ZoomBotApp
from voice.audio import resample_pcm
from voice.config import Settings
from voice.zoom import parse_meeting, url_validation_response, verify_webhook

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SPEECH_WAV = os.path.join(os.path.dirname(__file__), "fixtures", "speech.wav")

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

    async def start(self, zoom_api=False, notify=False, tts_max_seconds=1.5, llm=None, **overrides):
        self.runners = []
        self.llm_requests, self.zoom_log, self.notify_log = [], [], []
        runner, llm_base = await serve(fake_llm_app(self.llm_requests, llm))
        self.runners.append(runner)
        cfg = Settings()
        cfg.api_token = TOKEN
        cfg.llm_url = llm_base + "/v1/chat/completions"
        cfg.llm_api_key = ""
        cfg.filler_delay = 0
        cfg.join_timeout = 20
        cfg.personal_meeting_url = ""
        cfg.zoom_account_id = cfg.zoom_client_id = cfg.zoom_client_secret = ""
        cfg.zoom_webhook_secret = ""
        cfg.auto_join_meetings = ""
        cfg.notify_url = ""
        cfg.zoom_obf_token = ""
        cfg.always_on_meeting_url = ""
        if zoom_api:
            runner, zoom_base = await serve(fake_zoom_api_app(self.zoom_log))
            self.runners.append(runner)
            cfg.zoom_account_id, cfg.zoom_client_id, cfg.zoom_client_secret = "acct", "cid", "csecret"
            cfg.zoom_api_url, cfg.zoom_oauth_url = zoom_base + "/v2", zoom_base + "/oauth/token"
        if notify:
            runner, notify_base = await serve(fake_notify_app(self.notify_log))
            self.runners.append(runner)
            cfg.notify_url = notify_base + "/topic"
        for key, value in overrides.items():
            setattr(cfg, key, value)
        self.stt, self.tts = FakeSTT(), FakeTTS(max_seconds=tts_max_seconds)
        self.app = ZoomBotApp(cfg, self.stt, self.tts)
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
    env = await Env().start(zoom_api=True, notify=True)
    try:
        status, _ = await env.post("/api/call", {"reason": "The build finished."}, headers={})
        assert status == 401
        status, body = await env.post("/api/call", {"reason": "The build finished."})
        assert status == 503 and "bridge" in body["error"]

        bridge = await env.bridge()
        status, call = await env.post("/api/call", {"reason": "The build finished."})
        assert status == 200, call
        assert call["join_url"] == "https://us05web.zoom.us/j/98765432101?pwd=enc"
        assert await wait_for(lambda: bridge.commands_of("join"))
        join = bridge.commands_of("join")[0]
        assert join["meeting_number"] == "98765432101" and join["passcode"] == "s3cret"
        assert join["recording_token"] == "rec-token" and join["display_name"] == env.app.cfg.ai_name
        created = [e for e in env.zoom_log if e[0] == "create"][0][1]
        assert created["type"] == 1 and created["settings"]["join_before_host"] is True

        # You get the link on your phone.
        assert await wait_for(lambda: env.notify_log)
        assert env.notify_log[0]["click"] == call["join_url"] and call["join_url"] in env.notify_log[0]["text"]

        # A second call while this one is going is refused.
        status, body = await env.post("/api/call", {"reason": "again"})
        assert status == 409

        # The bot is in the meeting, alone: it waits quietly.
        bridge.start_audio()
        await bridge.join_meeting(others=0)
        await asyncio.sleep(0.5)
        assert env.llm_requests == [] and bridge.loud_frames == 0
        _, info = await env.get(f"/api/calls/{call['id']}")
        assert info["status"] == "waiting"

        # You join: it says why it called, and hangs up as the reply asks.
        await bridge.send(type="participants", others=1)
        assert await wait_for(lambda: env.llm_requests)
        system = env.llm_requests[0]["messages"][0]["content"]
        assert "The build finished." in system
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


async def _ai_calls_you_without_zoom_api():
    env = await Env().start(personal_meeting_url="https://zoom.us/j/1234567890?pwd=abcDEF",
                            zoom_obf_token="obf-from-settings")
    try:
        bridge = await env.bridge()
        status, call = await env.post("/api/call", {"reason": "Reminder"})
        assert status == 200
        assert call["join_url"] == "https://zoom.us/j/1234567890?pwd=abcDEF"
        assert await wait_for(lambda: bridge.commands_of("join"))
        join = bridge.commands_of("join")[0]
        assert (join["meeting_number"], join["passcode"], join["recording_token"]) == ("1234567890", "abcDEF", "")
        assert join["obf_token"] == "obf-from-settings"
    finally:
        await env.stop()


def test_ai_calls_you_without_zoom_api():
    run(_ai_calls_you_without_zoom_api())


async def _no_meeting_to_use():
    env = await Env().start()
    try:
        await env.bridge()
        status, body = await env.post("/api/call", {"reason": "x"})
        assert status == 400 and "PERSONAL_MEETING_URL" in body["error"]
    finally:
        await env.stop()


def test_no_meeting_to_use():
    run(_no_meeting_to_use())


# ---- you call it ----

async def _you_call_it():
    env = await Env().start()
    try:
        bridge = await env.bridge()
        status, body = await env.post("/api/join", {"link": "not a link"})
        assert status == 400
        status, call = await env.post("/api/join", {"link": "https://us05web.zoom.us/j/12345678901?pwd=abc",
                                                    "obf_token": "obf-for-this-call"})
        assert status == 200, call
        assert await wait_for(lambda: bridge.commands_of("join"))
        join = bridge.commands_of("join")[0]
        assert (join["meeting_number"], join["passcode"]) == ("12345678901", "abc")
        assert join["obf_token"] == "obf-for-this-call"

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
        await env.post("/api/join", {"link": "123456789"})
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


async def _asks_for_recording_permission():
    env = await Env().start()
    try:
        bridge = await env.bridge()
        await env.post("/api/join", {"link": "123456789"})
        bridge.start_audio()
        await bridge.join_meeting(recording=False)
        assert await wait_for(lambda: env.tts.spoken == [env.app.cfg.permission_prompt])
        await asyncio.sleep(0.5)
        assert env.llm_requests == [] and env.app.call.status == "waiting"
        # The host clicks Allow: now it greets you.
        await bridge.send(type="recording", allowed=True)
        assert await wait_for(lambda: env.app.cfg.greeting in env.tts.spoken)
        assert env.app.call.status == "in_call"
    finally:
        await env.stop()


def test_asks_for_recording_permission():
    run(_asks_for_recording_permission())


async def _nobody_comes():
    env = await Env().start(join_timeout=1)
    try:
        bridge = await env.bridge()
        _, call = await env.post("/api/join", {"link": "123456789"})
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
        _, call = await env.post("/api/join", {"link": "123456789"})
        await bridge.send(type="ended", failed=True, reason="couldn't join the meeting (Zoom error 3)")
        _, done = await env.get(f"/api/calls/{call['id']}?wait=1&timeout=5")
        assert done["status"] == "failed" and "Zoom error 3" in done["error"]

        # The bridge dropping mid-call ends the call; a new bridge can connect.
        _, call = await env.post("/api/join", {"link": "123456789"})
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
        _, call = await env.post("/api/join", {"link": "123456789"})
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


# ---- the Zoom webhook ----

def signed(secret, body, ts=None):
    ts = str(int(ts or time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    return {"x-zm-request-timestamp": ts, "x-zm-signature": sig, "Content-Type": "application/json"}


async def _webhook():
    secret = "whsecret"
    env = await Env().start(zoom_webhook_secret=secret, auto_join_meetings="111 222 3333:pw1, 444555666")
    try:
        bridge = await env.bridge()
        url = env.base + "/zoom/webhook"

        body = json.dumps({"event": "endpoint.url_validation", "payload": {"plainToken": "abc"}})
        async with env.http.post(url, data=body, headers=signed(secret, body)) as r:
            assert r.status == 200
            assert await r.json() == url_validation_response(secret, "abc")

        async with env.http.post(url, data=body, headers=signed("wrong", body)) as r:
            assert r.status == 401
        async with env.http.post(url, data=body, headers=signed(secret, body, ts=time.time() - 3600)) as r:
            assert r.status == 401

        # A meeting that isn't on the list: ignored.
        other = json.dumps({"event": "meeting.started", "payload": {"object": {"id": 999888777}}})
        async with env.http.post(url, data=other, headers=signed(secret, other)) as r:
            assert r.status == 200
        await asyncio.sleep(0.3)
        assert not bridge.commands_of("join")

        started = json.dumps({"event": "meeting.started",
                              "payload": {"object": {"id": 1112223333, "topic": "Standup"}}})
        async with env.http.post(url, data=started, headers=signed(secret, started)) as r:
            assert r.status == 200
        assert await wait_for(lambda: bridge.commands_of("join"))
        join = bridge.commands_of("join")[0]
        assert (join["meeting_number"], join["passcode"]) == ("1112223333", "pw1")
    finally:
        await env.stop()


def test_webhook():
    run(_webhook())


async def _webhook_off_without_secret():
    env = await Env().start()
    try:
        async with env.http.post(env.base + "/zoom/webhook", data="{}") as r:
            assert r.status == 404
    finally:
        await env.stop()


def test_webhook_off_without_secret():
    run(_webhook_off_without_secret())


# ---- small pieces ----

@pytest.mark.parametrize("link,expected", [
    ("https://zoom.us/j/1234567890?pwd=abc.1", ("1234567890", "abc.1")),
    ("https://us05web.zoom.us/j/98765432101?pwd=XyZ", ("98765432101", "XyZ")),
    ("acme.zoom.us/j/123456789", ("123456789", "")),
    ("zoommtg://zoom.us/join?action=join&confno=123456789&pwd=p", ("123456789", "p")),
    ("https://app.zoom.us/wc/join/123456789?pwd=q", ("123456789", "q")),
    ("123 4567 8901", ("12345678901", "")),
    ("123-456-789", ("123456789", "")),
])
def test_parse_meeting(link, expected):
    assert parse_meeting(link) == expected


@pytest.mark.parametrize("link", ["", "https://zoom.us/my/someone", "https://example.com/meeting", "12345"])
def test_parse_meeting_rejects(link):
    with pytest.raises(ValueError):
        parse_meeting(link)


def test_verify_webhook():
    body = b'{"a":1}'
    headers = signed("s", body.decode())
    ts, sig = headers["x-zm-request-timestamp"], headers["x-zm-signature"]
    assert verify_webhook("s", ts, body, sig)
    assert not verify_webhook("s", ts, b'{"a":2}', sig)
    assert not verify_webhook("", ts, body, sig)
    assert not verify_webhook("s", "soon", body, sig)


def test_auto_join_list():
    cfg = Settings()
    cfg.auto_join_meetings = "123 456 789:pw, 987654321 ,,"
    assert cfg.auto_join_list() == {"123456789": "pw", "987654321": ""}


# ---- always-on meeting ----

ALWAYS_ON = "https://zoom.us/j/5550001111?pwd=room"


async def _always_on():
    env = await Env().start(notify=True, always_on_meeting_url=ALWAYS_ON)
    try:
        # As soon as the bridge connects, the bot goes and waits in the always-on meeting.
        bridge = await env.bridge()
        assert await wait_for(lambda: bridge.commands_of("join"))
        join = bridge.commands_of("join")[0]
        assert (join["meeting_number"], join["passcode"]) == ("5550001111", "room")
        bridge.start_audio()
        await bridge.join_meeting(others=0)
        await asyncio.sleep(0.3)
        _, status = await env.get("/api/status")
        assert status["always_on"] == {"join_url": ALWAYS_ON, "bot_in_meeting": True}
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

        # "Call me" sends the always-on link instead of making a meeting.
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
        # Zoom ends the meeting (a time limit, or the host ended it). The bridge exits and restarts...
        await bridge.send(type="ended", failed=False, reason="the meeting ended")
        await bridge.close()
        # ...and the bot goes straight back.
        bridge = await env.bridge()
        assert await wait_for(lambda: bridge.commands_of("join"))
        assert bridge.commands_of("join")[0]["meeting_number"] == "5550001111"
        assert env.app.standby_failures == 0
    finally:
        await env.stop()


def test_always_on_rejoins():
    run(_always_on_rejoins())


async def _always_on_steps_out_for_another_meeting():
    env = await Env().start(always_on_meeting_url=ALWAYS_ON)
    try:
        bridge = await env.bridge()
        assert await wait_for(lambda: bridge.commands_of("join"))
        await bridge.join_meeting(others=0)

        # Asked to join a different meeting: it leaves the room first...
        status, call = await env.post("/api/join", {"link": "https://zoom.us/j/9998887777?pwd=x"})
        assert status == 200 and call["status"] == "joining" and call["always_on"] is False
        assert await wait_for(lambda: bridge.commands_of("leave"))
        assert len(bridge.commands_of("join")) == 1
        await bridge.close()  # the bridge restarts after leaving

        # ...joins the other meeting once the bridge is back...
        bridge = await env.bridge()
        assert await wait_for(lambda: bridge.commands_of("join"))
        assert bridge.commands_of("join")[0]["meeting_number"] == "9998887777"
        await bridge.join_meeting(others=1)
        assert await wait_for(lambda: env.app.cfg.greeting in env.tts.spoken)
        await bridge.send(type="participants", others=0)
        assert await wait_for(lambda: bridge.commands_of("leave"))
        _, done = await env.get(f"/api/calls/{call['id']}")
        assert done["status"] == "completed"
        await bridge.close()

        # ...and goes back to the always-on room afterwards.
        bridge = await env.bridge()
        assert await wait_for(lambda: bridge.commands_of("join"))
        assert bridge.commands_of("join")[0]["meeting_number"] == "5550001111"
    finally:
        await env.stop()


def test_always_on_steps_out_for_another_meeting():
    run(_always_on_steps_out_for_another_meeting())
