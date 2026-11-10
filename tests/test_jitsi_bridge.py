"""Jitsi bridge tests in real headless Chromium, against a fake Jitsi room.

The fake room offers the same APP.conference calls the real Jitsi Meet app has, and sends the
bot's microphone back to it over a real local WebRTC connection, so the whole audio path runs:
AI voice -> fake mic -> WebRTC -> remote track -> mixer -> bridge.
"""

import asyncio
import time
from urllib.parse import unquote

import numpy as np
import pytest

from jitsi_bridge.bridge import CHROMIUM_ARGS, JitsiBot, room_url

playwright_api = pytest.importorskip("playwright.async_api")

FAKE_ROOM = """<!doctype html><html><body><script>
window.__fake = { joined: false, members: 2, muted: true, joinAfter: JOIN_AFTER };
window.APP = { conference: {
    isJoined: () => __fake.joined,
    get membersCount() { return __fake.members; },
    isLocalAudioMuted: () => __fake.muted,
    async muteAudio(mute) {
        if (mute || !__fake.muted) { return; }
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        await loopback(stream.getAudioTracks()[0]);
        __fake.muted = false;
    },
    hangup() { __fake.joined = false; __fake.hungUp = true; }
} };
async function loopback(track) {
    const a = new RTCPeerConnection(), b = new RTCPeerConnection();
    a.onicecandidate = e => e.candidate && b.addIceCandidate(e.candidate);
    b.onicecandidate = e => e.candidate && a.addIceCandidate(e.candidate);
    a.addTrack(track, new MediaStream([ track ]));
    const offer = await a.createOffer();
    await a.setLocalDescription(offer);
    await b.setRemoteDescription(offer);
    const answer = await b.createAnswer();
    await b.setLocalDescription(answer);
    await a.setRemoteDescription(answer);
}
if (__fake.joinAfter >= 0) { setTimeout(() => { __fake.joined = true; }, __fake.joinAfter); }
</script></body></html>"""


def fake_jitsi(join_after_ms=300):
    async def on_context(context):
        async def serve(route):
            await route.fulfill(status=200, content_type="text/html",
                                body=FAKE_ROOM.replace("JOIN_AFTER", str(join_after_ms)))
        await context.route("https://jitsi.test/**", serve)
    return on_context


class Recorder:
    def __init__(self):
        self.messages, self.audio = [], []

    def __call__(self, message):
        (self.audio if isinstance(message, bytes) else self.messages).append(message)

    def of(self, kind):
        return [m for m in self.messages if m.get("type") == kind]

    def loud_chunks(self):
        return sum(1 for a in self.audio if np.abs(np.frombuffer(a, dtype=np.int16)).max() > 2000)


async def wait_for(predicate, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


def with_browser(test):
    async def run():
        async with playwright_api.async_playwright() as p:
            browser = await p.chromium.launch(args=CHROMIUM_ARGS)
            try:
                await test(browser)
            finally:
                await browser.close()
    asyncio.run(asyncio.wait_for(run(), 60))


JOIN = {"type": "join", "url": "https://jitsi.test/AssistantRoom", "display_name": "Max"}


def tone(seconds, rate=32000):
    t = np.arange(int(rate * seconds)) / rate
    return (np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16).tobytes()


def test_joins_and_relays_audio_both_ways():
    async def test(browser):
        rec = Recorder()
        bot = JitsiBot(browser, rec, poll_interval=0.1, on_context=fake_jitsi())
        await bot.join(JOIN)
        fragment = unquote(bot.page.url.split("#", 1)[1])
        assert 'config.prejoinConfig.enabled=false' in fragment and 'userInfo.displayName="Max"' in fragment
        assert await wait_for(lambda: rec.of("mic_ready"))
        kinds = [m["type"] for m in rec.messages]
        assert kinds.index("status") < kinds.index("recording") < kinds.index("mic_ready")
        assert {"type": "status", "status": "in_meeting"} in rec.messages
        assert {"type": "recording", "allowed": True} in rec.messages
        assert rec.of("participants") == [{"type": "participants", "others": 1}]

        # The AI talks for a second, in real time like the voice server sends it...
        pcm = tone(1.0)
        frame = 32000 * 2 // 50
        for i in range(0, len(pcm), frame):
            await bot.send_audio(pcm[i: i + frame])
            await asyncio.sleep(0.02)
        # ...and it comes back through the room's audio.
        assert await wait_for(lambda: rec.loud_chunks() >= 5), (len(rec.audio), rec.loud_chunks())

        # The other person leaves, then the bot leaves.
        await bot.page.evaluate("__fake.members = 1")
        assert await wait_for(lambda: rec.of("participants")[-1]["others"] == 0)
        page = bot.page
        hung_up = await page.evaluate("() => { APP.conference.hangup(); return __fake.hungUp; }")
        assert hung_up
        assert await wait_for(lambda: rec.of("ended"))
        assert rec.of("ended")[0] == {"type": "ended", "failed": False, "reason": "the meeting ended"}
        assert rec.messages[-1] == {"type": "hello"} and not bot.busy
    with_browser(test)


def test_leave():
    async def test(browser):
        rec = Recorder()
        bot = JitsiBot(browser, rec, poll_interval=0.1, on_context=fake_jitsi())
        await bot.join(JOIN)
        assert await wait_for(lambda: rec.of("mic_ready"))
        await bot.handle({"type": "leave"})
        assert rec.of("ended") == [{"type": "ended", "failed": False, "reason": "left"}]
        assert rec.messages[-1] == {"type": "hello"} and not bot.busy
        # It can join again afterwards (the always-on room).
        await bot.join(JOIN)
        assert await wait_for(lambda: len(rec.of("mic_ready")) == 2)
        await bot.leave()
    with_browser(test)


def test_join_timeout_and_bad_links():
    async def test(browser):
        rec = Recorder()
        bot = JitsiBot(browser, rec, poll_interval=0.1, join_timeout=1, on_context=fake_jitsi(join_after_ms=-1))
        await bot.join(JOIN)
        assert await wait_for(lambda: rec.of("ended"))
        assert rec.of("ended")[0]["failed"] is True and "timed out" in rec.of("ended")[0]["reason"]
        assert not bot.busy

        await bot.join({"type": "join", "url": "AssistantRoom"})
        assert rec.of("ended")[-1]["failed"] is True and "not a Jitsi room link" in rec.of("ended")[-1]["reason"]

        # A server that isn't there.
        bot = JitsiBot(browser, rec, poll_interval=0.1)
        await bot.join({"type": "join", "url": "https://127.0.0.1:9/Room"})
        assert "couldn't open the room" in rec.of("ended")[-1]["reason"]
    with_browser(test)


def test_second_join_is_refused():
    async def test(browser):
        rec = Recorder()
        bot = JitsiBot(browser, rec, poll_interval=0.1, on_context=fake_jitsi())
        await bot.join(JOIN)
        await bot.join(JOIN)
        assert rec.of("error") == [{"type": "error", "message": "already in a meeting"}]
        await bot.leave()
    with_browser(test)


def test_room_url():
    url = room_url("https://meet.example.com/Room#old=1", "Max", internal_base="https://localhost:8443")
    assert url.startswith("https://localhost:8443/Room#")
    assert "old=1" not in url
    assert 'userInfo.displayName=%22Max%22' in url and "config.p2p.enabled=false" in url
