"""The real voice server and the real Jitsi bridge (in real Chromium) together, in a fake room."""

import asyncio

import pytest

from jitsi_bridge.bridge import CHROMIUM_ARGS, JitsiBot, run_link
from tests.test_jitsi_bridge import fake_jitsi
from tests.test_voice import TOKEN, Env, wait_for

playwright_api = pytest.importorskip("playwright.async_api")


async def _end_to_end():
    env = await Env().start()
    async with playwright_api.async_playwright() as p:
        browser = await p.chromium.launch(args=CHROMIUM_ARGS)
        heard = []
        link = asyncio.ensure_future(run_link(
            env.base.replace("http", "ws") + "/bridge", TOKEN,
            lambda emit: JitsiBot(browser, emit, poll_interval=0.1, on_context=fake_jitsi())))
        try:
            assert await wait_for(lambda: env.app.bridge is not None)
            status, call = await env.post("/api/join", {"link": "https://jitsi.test/AssistantRoom"})
            assert status == 200, call

            # The bot joins the room, someone is there, and it greets them through its "mic".
            assert await wait_for(lambda: env.app.call and env.app.call.status == "in_call", timeout=20)
            original = env.app.call.on_audio
            env.app.call.on_audio = lambda pcm: (heard.append(pcm), original(pcm))
            assert await wait_for(lambda: env.app.cfg.greeting in env.tts.spoken)
            # The room's audio reaches the voice server (here: the bot's own voice, looped back).
            assert await wait_for(lambda: len(heard) > 20, timeout=15)

            # Hanging up from the API makes the bot leave the room.
            status, info = await env.post(f"/api/calls/{call['id']}/hangup")
            assert status == 200 and info["status"] == "completed"
            assert await wait_for(lambda: env.app.meeting is None, timeout=10)
        finally:
            link.cancel()
            await asyncio.gather(link, return_exceptions=True)
            await browser.close()
            await env.stop()


def test_end_to_end():
    asyncio.run(asyncio.wait_for(_end_to_end(), 90))
