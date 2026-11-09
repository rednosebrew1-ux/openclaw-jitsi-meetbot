"""HTTP API, the bridge connection, and the life of each Zoom call."""

import asyncio
import hmac
import json
import logging
import secrets
import time

import aiohttp
from aiohttp import web

from .audio import Speaker
from .conversation import Conversation
from .zoom import ZoomAPI, ZoomError, parse_meeting, url_validation_response, verify_webhook

log = logging.getLogger(__name__)

KEEP_CALLS = 50


class Call:
    """One Zoom call, from "join" to the bot leaving."""

    GREET_DELAY = 1.5  # seconds after someone joins, so their audio is connected before we talk

    def __init__(self, app, kind, number, passcode, join_url="", reason=None):
        self.app = app
        self.id = secrets.token_hex(6)
        self.kind = kind                    # "ai_call" (the AI called you) or "join" (you called it)
        self.number, self.passcode, self.join_url = number, passcode, join_url
        self.reason = reason
        self.status = "joining"             # joining, waiting, in_call, completed, missed, failed
        self.error = None
        self.created = time.time()
        self.ended_at = None
        self.done = asyncio.Event()
        self.present = False                # someone other than the bot is in the meeting
        self.mic_ready = False
        self.hearing = False                # Zoom lets the bot hear the meeting
        self.greeted = False
        self.asked_permission = False
        self._timer = None
        self._greet_task = None
        cfg = app.cfg
        self.speaker = Speaker(app.send_audio)
        self.speaker.start()
        self.conversation = Conversation(cfg, app.stt, app.tts, self.speaker, self.id,
                                         reason=reason, on_hangup=self.hang_up)

    def info(self):
        return {
            "id": self.id, "kind": self.kind, "status": self.status, "meeting": self.number,
            "join_url": self.join_url, "reason": self.reason, "error": self.error,
            "transcript": self.conversation.transcript,
            "duration": round((self.ended_at or time.time()) - self.created, 1),
        }

    @property
    def finished(self):
        return self.done.is_set()

    def start_timer(self):
        self._timer = asyncio.get_running_loop().call_later(
            self.app.cfg.join_timeout, lambda: asyncio.ensure_future(self._nobody_came()))

    async def _nobody_came(self):
        if not self.present and not self.finished:
            log.info("[%s] nobody joined within %.0fs; leaving", self.id, self.app.cfg.join_timeout)
            await self.finish("missed", leave=True)

    async def hang_up(self):
        log.info("[%s] hanging up", self.id)
        await self.finish("completed", leave=True)

    # ---- events from the bridge ----

    async def on_event(self, event):
        kind = event.get("type")
        if kind == "status":
            status = event.get("status")
            log.info("[%s] zoom: %s", self.id, status)
            if status == "in_meeting" and self.status == "joining":
                self.status = "waiting"
        elif kind == "mic_ready":
            self.mic_ready = True
        elif kind == "recording":
            self.hearing = bool(event.get("allowed"))
            log.info("[%s] %s hear the meeting", self.id, "can" if self.hearing else "can't")
        elif kind == "participants":
            others = int(event.get("others") or 0)
            if others == 0 and self.present:
                log.info("[%s] everyone else left", self.id)
                await self.finish("completed", leave=True)
                return
            self.present = others > 0
        elif kind == "ended":
            reason = event.get("reason") or "ended"
            log.info("[%s] bot left the meeting (%s)", self.id, reason)
            failed = event.get("failed")
            if failed:
                self.error = reason
            await self.finish("failed" if failed else ("completed" if self.greeted else "missed"))
            return
        elif kind == "error":
            self.error = event.get("message")
            log.warning("[%s] bridge error: %s", self.id, self.error)
        self._maybe_greet()

    def _maybe_greet(self):
        if self.finished or self.greeted or not (self.present and self.mic_ready):
            return
        if self.hearing:
            if not self._greet_task:
                self._greet_task = asyncio.ensure_future(self._greet())
        elif not self.asked_permission:
            self.asked_permission = True
            self.conversation.say(self.app.cfg.permission_prompt)

    async def _greet(self):
        await asyncio.sleep(self.GREET_DELAY)
        if self.finished:
            return
        self.greeted = True
        self.status = "in_call"
        self.conversation.greet()

    def on_audio(self, pcm):
        self.conversation.feed(pcm)

    # ---- the end ----

    async def finish(self, status, leave=False):
        if self.finished:
            return
        self.status = status
        self.ended_at = time.time()
        if self._timer:
            self._timer.cancel()
        if self._greet_task and not self._greet_task.done():
            self._greet_task.cancel()
        self.done.set()
        await self.conversation.close()
        if leave:
            await self.app.send_bridge({"type": "leave"})
        self.app.call_finished(self)
        log.info("[%s] call %s after %.0fs", self.id, status, self.ended_at - self.created)


class ZoomCallerApp:
    def __init__(self, cfg, stt, tts):
        self.cfg, self.stt, self.tts = cfg, stt, tts
        self.zoom = ZoomAPI(cfg) if cfg.zoom_api_enabled else None
        self.bridge = None          # the bridge's WebSocket
        self.call = None            # the active call, if any
        self.calls = {}             # recent calls by id
        self._tasks = set()

    # ---- bridge link ----

    async def send_bridge(self, message):
        ws = self.bridge
        if ws is not None and not ws.closed:
            try:
                await ws.send_json(message)
            except (ConnectionError, RuntimeError) as e:
                log.debug("couldn't send to bridge: %r", e)

    async def send_audio(self, pcm):
        ws = self.bridge
        if ws is not None and not ws.closed and self.call is not None:
            await ws.send_bytes(pcm)

    async def bridge_ws(self, request):
        if not self._authorized(request):
            raise web.HTTPUnauthorized()
        if self.bridge is not None and not self.bridge.closed:
            raise web.HTTPConflict(text="another bridge is already connected")
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        self.bridge = ws
        log.info("Zoom bridge connected")
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    if self.call is not None:
                        self.call.on_audio(msg.data)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        event = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    if self.call is not None:
                        await self.call.on_event(event)
        finally:
            if self.bridge is ws:
                self.bridge = None
            log.info("Zoom bridge disconnected")
            if self.call is not None:
                self.call.error = self.call.error or "the Zoom bridge disconnected"
                await self.call.finish("completed" if self.call.greeted else "failed")
        return ws

    # ---- starting calls ----

    def call_finished(self, call):
        if self.call is call:
            self.call = None

    async def start_call(self, kind, number, passcode, join_url="", reason=None):
        """Send the bot into a meeting. Raises web.HTTPException if it can't."""
        if self.bridge is None or self.bridge.closed:
            raise web.HTTPServiceUnavailable(text=json.dumps({"error": "the Zoom bridge isn't connected"}),
                                             content_type="application/json")
        if self.call is not None:
            raise web.HTTPConflict(text=json.dumps({"error": "already on a call", "call": self.call.id}),
                                   content_type="application/json")
        call = Call(self, kind, number, passcode, join_url, reason)
        self.call = call
        self.calls[call.id] = call
        while len(self.calls) > KEEP_CALLS:
            self.calls.pop(next(iter(self.calls)))
        token = await self.zoom.recording_token(number) if self.zoom else None
        call.start_timer()
        await self.send_bridge({
            "type": "join", "meeting_number": number, "passcode": passcode,
            "display_name": self.cfg.ai_name, "recording_token": token or "",
        })
        log.info("[%s] joining meeting %s (%s)", call.id, number, kind)
        return call

    def _personal_meeting(self):
        if not self.cfg.personal_meeting_url:
            return None
        number, passcode = parse_meeting(self.cfg.personal_meeting_url)
        return number, passcode, self.cfg.personal_meeting_url

    async def call_user(self, request):
        """The AI calls you: it opens a meeting, joins it, and sends you the link."""
        self._require_auth(request)
        body = await self._json(request)
        reason = (body.get("reason") or "").strip() or None
        if self.zoom:
            try:
                meeting = await self.zoom.create_meeting(body.get("topic") or f"Call from {self.cfg.ai_name}")
            except (ZoomError, aiohttp.ClientError) as e:
                log.error("creating a Zoom meeting failed: %s", e)
                return self._error(502, f"couldn't create a Zoom meeting: {e}")
            number, passcode, join_url = str(meeting["id"]), meeting.get("password", ""), meeting["join_url"]
        else:
            personal = self._personal_meeting()
            if not personal:
                return self._error(400, "set PERSONAL_MEETING_URL, or the ZOOM_ACCOUNT_ID/ZOOM_CLIENT_ID/"
                                        "ZOOM_CLIENT_SECRET Zoom API settings, so the bot has a meeting to use")
            number, passcode, join_url = personal
        call = await self.start_call("ai_call", number, passcode, join_url, reason)
        self._spawn(self._notify(call))
        return web.json_response(call.info())

    async def join(self, request):
        """You called it: the bot joins your meeting (your personal meeting if no link is given)."""
        self._require_auth(request)
        body = await self._json(request)
        link = (body.get("link") or "").strip()
        try:
            if link:
                number, passcode = parse_meeting(link)
                join_url = link if "://" in link else ""
            else:
                personal = self._personal_meeting()
                if not personal:
                    return self._error(400, "no meeting link given, and PERSONAL_MEETING_URL isn't set")
                number, passcode, join_url = personal
        except ValueError as e:
            return self._error(400, str(e))
        if not passcode and self.zoom:
            passcode = await self._lookup_passcode(number)
        call = await self.start_call("join", number, passcode, join_url, (body.get("reason") or "").strip() or None)
        return web.json_response(call.info())

    async def _lookup_passcode(self, number):
        try:
            return (await self.zoom.get_meeting(number)).get("password", "")
        except (ZoomError, aiohttp.ClientError) as e:
            log.info("couldn't look up meeting %s's passcode: %s", number, e)
            return ""

    async def _notify(self, call):
        if not self.cfg.notify_url:
            return
        text = f"{self.cfg.ai_name} is calling you on Zoom. Tap to join: {call.join_url}"
        headers = {"Title": f"Zoom call from {self.cfg.ai_name}", "Click": call.join_url, "Priority": "high",
                   "Tags": "telephone_receiver"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.post(self.cfg.notify_url, data=text.encode(), headers=headers) as r:
                    if r.status >= 400:
                        log.warning("notification failed: %s %s", r.status, (await r.text())[:200])
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("notification failed: %r", e)

    # ---- call status ----

    async def get_call(self, request):
        self._require_auth(request)
        call = self.calls.get(request.match_info["id"])
        if call is None:
            raise web.HTTPNotFound(text=json.dumps({"error": "no such call"}), content_type="application/json")
        if request.query.get("wait") and not call.finished:
            timeout = min(float(request.query.get("timeout", 600)), 3600)
            try:
                await asyncio.wait_for(call.done.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        return web.json_response(call.info())

    async def hangup_call(self, request):
        self._require_auth(request)
        call = self.calls.get(request.match_info["id"])
        if call is None:
            raise web.HTTPNotFound(text=json.dumps({"error": "no such call"}), content_type="application/json")
        await call.finish("completed" if call.greeted else "missed", leave=True)
        return web.json_response(call.info())

    async def status(self, request):
        self._require_auth(request)
        return web.json_response({
            "bridge_connected": self.bridge is not None and not self.bridge.closed,
            "call": self.call.info() if self.call else None,
            "zoom_api": self.zoom is not None,
            "personal_meeting": bool(self.cfg.personal_meeting_url),
        })

    async def healthz(self, request):
        return web.json_response({"ok": True, "bridge_connected": self.bridge is not None and not self.bridge.closed})

    # ---- Zoom webhook (optional): join when one of your meetings starts ----

    async def webhook(self, request):
        secret = self.cfg.zoom_webhook_secret
        if not secret:
            raise web.HTTPNotFound()
        body = await request.read()
        if not verify_webhook(secret, request.headers.get("x-zm-request-timestamp"), body,
                              request.headers.get("x-zm-signature")):
            raise web.HTTPUnauthorized()
        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            raise web.HTTPBadRequest()
        payload = event.get("payload") or {}
        if event.get("event") == "endpoint.url_validation":
            return web.json_response(url_validation_response(secret, str(payload.get("plainToken", ""))))
        if event.get("event") == "meeting.started":
            meeting = payload.get("object") or {}
            number = str(meeting.get("id", ""))
            wanted = self.cfg.auto_join_list()
            if number in wanted:
                self._spawn(self._auto_join(number, wanted[number], meeting.get("topic", "")))
        return web.json_response({"ok": True})

    async def _auto_join(self, number, passcode, topic):
        if not passcode and self.zoom:
            passcode = await self._lookup_passcode(number)
        log.info("meeting %s (%s) started; joining", number, topic)
        try:
            await self.start_call("join", number, passcode)
        except web.HTTPException as e:
            log.warning("couldn't auto-join meeting %s: %s", number, e.text)

    # ---- helpers ----

    def _authorized(self, request):
        token = self.cfg.api_token
        header = request.headers.get("Authorization", "")
        return bool(token) and hmac.compare_digest(header.encode(), f"Bearer {token}".encode())

    def _require_auth(self, request):
        if not self._authorized(request):
            raise web.HTTPUnauthorized(text=json.dumps({"error": "missing or wrong API token"}),
                                       content_type="application/json")

    @staticmethod
    async def _json(request):
        if not request.can_read_body:
            return {}
        try:
            data = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text=json.dumps({"error": "body must be JSON"}),
                                     content_type="application/json")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _error(status, message):
        return web.json_response({"error": message}, status=status)

    def _spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def on_shutdown(self, app):
        if self.call is not None:
            await self.call.finish("completed" if self.call.greeted else "missed", leave=True)
        if self.bridge is not None:
            await self.bridge.close()

    def make_app(self):
        app = web.Application()
        app.router.add_get("/healthz", self.healthz)
        app.router.add_get("/bridge", self.bridge_ws)
        app.router.add_get("/api/status", self.status)
        app.router.add_post("/api/call", self.call_user)
        app.router.add_post("/api/join", self.join)
        app.router.add_get("/api/calls/{id}", self.get_call)
        app.router.add_post("/api/calls/{id}/hangup", self.hangup_call)
        app.router.add_post("/zoom/webhook", self.webhook)
        app.on_shutdown.append(self.on_shutdown)
        return app
