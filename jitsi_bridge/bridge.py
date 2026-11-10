"""Jitsi bridge: joins a Jitsi Meet room in headless Chromium and relays its audio.

The room's audio (everyone but the bot, mixed, 32 kHz 16-bit mono) goes to the voice server over a
WebSocket, and audio the voice server sends back is the bot's microphone.
Runs anywhere Playwright's Chromium does, including natively on Apple Silicon Macs.

Run: python -m jitsi_bridge
"""

import asyncio
import base64
import json
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

log = logging.getLogger("jitsi_bridge")

AUDIO_JS = (Path(__file__).with_name("audio.js")).read_text()

# Jitsi settings applied through the room URL (all on Jitsi's allow-list for URL overrides).
ROOM_SETTINGS = {
    "config.prejoinConfig.enabled": False,   # skip the "join meeting" screen
    "config.p2p.enabled": False,             # one media path, so audio isn't heard twice
    "config.startWithVideoMuted": True,
    "config.disableAP": True,                # no echo cancellation or noise suppression on the AI's voice
    "config.disableDeepLinking": True,       # don't offer the mobile app
    "config.enableNoAudioDetection": False,
    "config.enableNoisyMicDetection": False,
    "config.requireDisplayName": False,
}

CHROMIUM_ARGS = [
    "--use-fake-ui-for-media-stream",        # allow the "microphone" without asking
    "--use-fake-device-for-media-stream",    # so the page sees a microphone device at all
    "--autoplay-policy=no-user-gesture-required",
]


def room_url(url, display_name, internal_base=""):
    """The room URL with the bot's settings in the #fragment (and the host swapped if asked)."""
    parts = urlsplit(url)
    if internal_base:
        base = urlsplit(internal_base)
        parts = parts._replace(scheme=base.scheme, netloc=base.netloc)
    params = dict(ROOM_SETTINGS)
    params["userInfo.displayName"] = display_name or "Assistant"
    fragment = "&".join(f"{key}={quote(json.dumps(value))}" for key, value in params.items())
    return urlunsplit(parts._replace(fragment=fragment))


class JitsiBot:
    """One room at a time. Everything runs on the asyncio loop."""

    def __init__(self, browser, emit, ignore_tls_errors=False, internal_base="", join_timeout=60,
                 poll_interval=0.5, on_context=None):
        self.browser = browser
        self.emit = emit                     # dict -> JSON message, bytes -> audio
        self.ignore_tls_errors = ignore_tls_errors
        self.internal_base = internal_base
        self.join_timeout = join_timeout
        self.poll_interval = poll_interval
        self.on_context = on_context         # async hook(context), used by tests to serve fake rooms
        self.context = self.page = None
        self.mic_ready = False
        self._watcher = None
        self._ending = False

    @property
    def busy(self):
        return self.page is not None

    async def handle(self, message):
        kind = message.get("type")
        if kind == "join":
            await self.join(message)
        elif kind == "leave":
            await self.leave()

    async def join(self, message):
        if self.busy:
            self.emit({"type": "error", "message": "already in a meeting"})
            return
        url = message.get("url") or message.get("meeting_number") or ""
        if not url.startswith(("http://", "https://")):
            self.emit({"type": "ended", "failed": True, "reason": f"not a Jitsi room link: {url!r}"})
            self.emit({"type": "hello"})
            return
        self._ending = False
        self.mic_ready = False
        self.context = await self.browser.new_context(ignore_https_errors=self.ignore_tls_errors)
        if self.on_context:
            await self.on_context(self.context)
        await self.context.expose_function("__aiAudioOut", self._audio_out)
        await self.context.add_init_script(script=AUDIO_JS)
        self.page = await self.context.new_page()
        self.page.on("crash", lambda _: asyncio.ensure_future(self._finish(True, "the browser page crashed")))
        self.emit({"type": "status", "status": "joining"})
        log.info("joining %s", url)
        try:
            await self.page.goto(room_url(url, message.get("display_name"), self.internal_base),
                                 wait_until="domcontentloaded", timeout=30000)
        except Exception as e:  # Playwright raises its own error types for network/TLS failures
            await self._finish(True, f"couldn't open the room: {str(e).splitlines()[0]}")
            return
        self._watcher = asyncio.ensure_future(self._watch())

    async def _watch(self):
        start, joined, others = time.monotonic(), False, None
        last_unmute = 0.0
        while self.page is not None and not self._ending:
            try:
                state = await self.page.evaluate("window.__ai ? window.__ai.state() : null")
            except Exception:
                state = None
                if self.page is None or self.page.is_closed():
                    break
            if state and state["joined"]:
                if not joined:
                    joined = True
                    log.info("in the room")
                    self.emit({"type": "status", "status": "in_meeting"})
                    self.emit({"type": "recording", "allowed": True})  # nothing to ask permission for
                n = max(0, int(state["members"]) - 1)
                if n != others:
                    others = n
                    self.emit({"type": "participants", "others": n})
                if not state["muted"] and not self.mic_ready:
                    self.mic_ready = True
                    self.emit({"type": "mic_ready"})
                elif state["muted"] and time.monotonic() - last_unmute > 5:
                    # Starts muted (the pre-join screen is skipped), or someone muted the bot.
                    last_unmute = time.monotonic()
                    try:
                        await self.page.evaluate("APP.conference.muteAudio(false)")
                    except Exception as e:
                        log.debug("unmuting failed: %r", e)
            elif joined:
                await self._finish(False, "the meeting ended")
                return
            elif time.monotonic() - start > self.join_timeout:
                await self._finish(True, "couldn't join the room (timed out); is the Jitsi server reachable?")
                return
            await asyncio.sleep(self.poll_interval)
        if not self._ending:
            await self._finish(not joined, "the browser page closed")

    def _audio_out(self, b64):
        self.emit(base64.b64decode(b64))

    async def send_audio(self, pcm):
        if self.page is None or not self.mic_ready:
            return
        try:
            await self.page.evaluate("b => window.__ai.play(b)", base64.b64encode(pcm).decode())
        except Exception as e:
            log.debug("sending audio failed: %r", e)

    async def leave(self):
        if self.page is None:
            return
        log.info("leaving the room")
        try:
            await asyncio.wait_for(self.page.evaluate("APP.conference.hangup(false)"), 5)
        except Exception as e:
            log.debug("hangup failed: %r", e)
        await self._finish(False, "left")

    async def _finish(self, failed, reason):
        if self._ending:
            return
        self._ending = True
        log.info("meeting over: %s", reason)
        watcher, self._watcher = self._watcher, None
        if watcher and watcher is not asyncio.current_task():
            watcher.cancel()
        context, self.context, self.page = self.context, None, None
        self.mic_ready = False
        if context is not None:
            try:
                await context.close()
            except Exception as e:
                log.debug("closing the browser context failed: %r", e)
        self.emit({"type": "ended", "failed": failed, "reason": reason})
        self.emit({"type": "hello"})  # ready for the next meeting


async def run_link(url, token, bot_factory):
    """Stay connected to the voice server; run a JitsiBot for its commands."""
    delay = 1
    while True:
        outbox = asyncio.Queue()

        def emit(message):
            outbox.put_nowait(message)

        bot = bot_factory(emit)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url, heartbeat=20,
                                              headers={"Authorization": f"Bearer {token}"}) as ws:
                    log.info("connected to the voice server at %s", url)
                    delay = 1

                    async def pump():
                        while True:
                            message = await outbox.get()
                            if isinstance(message, bytes):
                                await ws.send_bytes(message)
                            else:
                                await ws.send_json(message)

                    sender = asyncio.ensure_future(pump())
                    emit({"type": "hello"})
                    try:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.BINARY:
                                await bot.send_audio(msg.data)
                            elif msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    message = json.loads(msg.data)
                                except json.JSONDecodeError:
                                    continue
                                asyncio.ensure_future(bot.handle(message))
                    finally:
                        sender.cancel()
                        # The voice server lost track of this call: don't sit in the room silently.
                        await bot.leave()
        except (aiohttp.ClientError, OSError) as e:
            log.warning("can't reach the voice server at %s (%s); retrying in %ss", url, e, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)


def _env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


async def amain():
    from playwright.async_api import async_playwright

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from voice.config import load_dotenv  # same .env as the voice server

    load_dotenv()
    if not os.environ.get("API_TOKEN"):
        log.error("API_TOKEN is not set (see .env.example)")
        sys.exit(1)
    url = os.environ.get("VOICE_URL", f"ws://127.0.0.1:{os.environ.get('PORT', '8090')}/bridge")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=_env_bool("JITSI_HEADLESS", True), args=CHROMIUM_ARGS)
        log.info("Jitsi bridge ready")

        def factory(emit):
            return JitsiBot(browser, emit,
                            ignore_tls_errors=_env_bool("JITSI_IGNORE_TLS_ERRORS"),
                            internal_base=os.environ.get("JITSI_INTERNAL_URL", ""))

        await run_link(url, os.environ["API_TOKEN"], factory)


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
