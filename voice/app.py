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


class Meeting:
    """The meeting the bridge is in (or joining). In always-on mode it outlives many calls."""

    def __init__(self, number, passcode, join_url="", standby=False, obf_token=""):
        self.number, self.passcode, self.join_url = number, passcode, join_url
        self.standby = standby              # the always-on meeting: the bot stays when people leave
        self.obf_token = obf_token
        self.in_meeting = False
        self.mic_ready = False
        self.hearing = False                # Zoom lets the bot hear the meeting
        self.others = 0                     # people in the meeting besides the bot
        self.leaving = False
        # In always-on mode, a new call starts when someone arrives. After a call ends while people
        # are still there (the AI said goodbye), wait until the room empties before starting another.
        self.call_blocked = False


class Call:
    """One conversation in a Zoom meeting, from the bot joining (or someone arriving) to the end."""

    GREET_DELAY = 1.5  # seconds after someone joins, so their audio is connected before we talk

    def __init__(self, app, kind, meeting, reason=None):
        self.app = app
        self.id = secrets.token_hex(6)
        self.kind = kind                    # "ai_call" (the AI called you), "join" (you called it),
                                            # or "standby" (you joined the always-on meeting)
        self.meeting = meeting
        self.reason = reason
        self.status = "waiting" if meeting.in_meeting else "joining"
        self.error = None
        self.created = time.time()
        self.ended_at = None
        self.done = asyncio.Event()
        self.present = meeting.in_meeting and meeting.others > 0
        self.greeted = False
        self.asked_permission = False
        self._timer = None
        self._greet_task = None
        cfg = app.cfg
        self.speaker = Speaker(app.send_audio)
        self.speaker.start()
        self.conversation = Conversation(cfg, app.stt, app.tts, self.speaker, self.id,
                                         reason=reason, on_hangup=self.hang_up)

    @property
    def number(self):
        return self.meeting.number

    @property
    def join_url(self):
        return self.meeting.join_url

    def info(self):
        return {
            "id": self.id, "kind": self.kind, "status": self.status, "meeting": self.number,
            "join_url": self.join_url, "always_on": self.meeting.standby, "reason": self.reason,
            "error": self.error, "transcript": self.conversation.transcript,
            "duration": round((self.ended_at or time.time()) - self.created, 1),
        }

    @property
    def finished(self):
        return self.done.is_set()

    def start_timer(self):
        if self.present:
            return
        self._timer = asyncio.get_running_loop().call_later(
            self.app.cfg.join_timeout, lambda: asyncio.ensure_future(self._nobody_came()))

    async def _nobody_came(self):
        if not self.present and not self.finished:
            log.info("[%s] nobody joined within %.0fs", self.id, self.app.cfg.join_timeout)
            await self.finish("missed", leave=True)

    async def hang_up(self):
        log.info("[%s] hanging up", self.id)
        await self.finish("completed", leave=True)

    # ---- events from the bridge (the meeting's state is already updated) ----

    async def on_event(self, event):
        kind = event.get("type")
        if kind == "status":
            if event.get("status") == "in_meeting" and self.status == "joining":
                self.status = "waiting"
        elif kind == "participants":
            others = self.meeting.others
            if others == 0 and self.present:
                log.info("[%s] everyone else left", self.id)
                await self.finish("completed" if self.greeted else "missed", leave=True)
                return
            self.present = others > 0
        elif kind == "ended":
            reason = event.get("reason") or "ended"
            failed = event.get("failed")
            if failed:
                self.error = reason
            await self.finish("failed" if failed else ("completed" if self.greeted else "missed"))
            return
        elif kind == "error":
            self.error = event.get("message")
        self.maybe_greet()

    def maybe_greet(self):
        m = self.meeting
        if self.finished or self.greeted or not (self.present and m.mic_ready):
            return
        if m.hearing:
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
        """End this conversation. `leave` makes the bot leave too, except in the always-on meeting."""
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
        if self.meeting.standby:
            self.meeting.call_blocked = self.meeting.others > 0
        elif leave:
            await self.app.leave_meeting(self.meeting)
        self.app.call_finished(self)
        log.info("[%s] call %s after %.0fs", self.id, status, self.ended_at - self.created)


class ZoomBotApp:
    def __init__(self, cfg, stt, tts):
        self.cfg, self.stt, self.tts = cfg, stt, tts
        self.zoom = ZoomAPI(cfg) if cfg.zoom_api_enabled else None
        self.bridge = None          # the bridge's WebSocket
        self.meeting = None         # the meeting the bridge is in or joining
        self.call = None            # the active call, if any
        self.pending = None         # a call waiting for the bridge to restart before it can join
        self.calls = {}             # recent calls by id
        self.standby_failures = 0
        self._tasks = set()
        self.standby = None
        if cfg.always_on_meeting_url:
            number, passcode = parse_meeting(cfg.always_on_meeting_url)
            self.standby = (number, passcode, cfg.always_on_meeting_url)

    # ---- bridge link ----

    @property
    def bridge_connected(self):
        return self.bridge is not None and not self.bridge.closed

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
        if self.bridge_connected:
            raise web.HTTPConflict(text="another bridge is already connected")
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        self.bridge = ws
        log.info("Zoom bridge connected")
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    if self.call is not None and self.call.meeting is self.meeting:
                        self.call.on_audio(msg.data)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        event = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "hello":
                        self._spawn(self._bridge_ready())
                    else:
                        await self._on_meeting_event(event)
        finally:
            if self.bridge is ws:
                self.bridge = None
            log.info("Zoom bridge disconnected")
            self.meeting = None
            call = self.call
            if call is not None and call is not self.pending:
                call.error = call.error or "the Zoom bridge disconnected"
                await call.finish("completed" if call.greeted else "failed")
        return ws

    async def _on_meeting_event(self, event):
        m = self.meeting
        if m is None:
            return
        kind = event.get("type")
        if kind == "status":
            log.info("zoom: %s", event.get("status"))
            if event.get("status") == "in_meeting":
                m.in_meeting = True
                if m.standby:
                    self.standby_failures = 0
                    log.info("in the always-on meeting %s; join it any time to talk", m.number)
        elif kind == "mic_ready":
            m.mic_ready = True
        elif kind == "recording":
            m.hearing = bool(event.get("allowed"))
            log.info("%s hear the meeting", "can" if m.hearing else "can't")
        elif kind == "participants":
            m.others = int(event.get("others") or 0)
            if m.others == 0:
                m.call_blocked = False
        elif kind == "ended":
            log.info("bot left the meeting (%s)", event.get("reason") or "ended")
            if m.standby and (event.get("failed") or not m.in_meeting):
                self.standby_failures += 1
            self.meeting = None
        elif kind == "error":
            log.warning("bridge error: %s", event.get("message"))

        call = self.call
        if call is not None and call.meeting is m:
            await call.on_event(event)
        if (m.standby and self.meeting is m and self.call is None and m.in_meeting
                and m.others > 0 and not m.call_blocked):
            # Someone joined the always-on meeting: start a conversation.
            log.info("someone joined the always-on meeting")
            self._new_call("standby", m).maybe_greet()

    async def _bridge_ready(self):
        """The bridge (re)connected and is idle: send any waiting call, or go back to the always-on meeting."""
        if self.pending is not None:
            call, self.pending = self.pending, None
            if not call.finished:
                await self._send_join(call.meeting)
            return
        if not self.standby or self.meeting is not None or self.call is not None:
            return
        if self.standby_failures:
            delay = min(300, 5 * 2 ** (self.standby_failures - 1))
            log.info("rejoining the always-on meeting in %ss", delay)
            await asyncio.sleep(delay)
            if self.meeting is not None or self.call is not None or not self.bridge_connected:
                return
        number, passcode, url = self.standby
        await self._send_join(Meeting(number, passcode, url, standby=True))

    async def _send_join(self, meeting):
        token = await self.zoom.recording_token(meeting.number) if self.zoom else None
        self.meeting = meeting
        await self.send_bridge({
            "type": "join", "meeting_number": meeting.number, "passcode": meeting.passcode,
            "display_name": self.cfg.ai_name, "recording_token": token or "",
            "obf_token": meeting.obf_token or self.cfg.zoom_obf_token,
        })
        log.info("joining meeting %s%s", meeting.number, " (always on)" if meeting.standby else "")

    async def leave_meeting(self, meeting):
        if self.meeting is meeting and not meeting.leaving:
            meeting.leaving = True
            await self.send_bridge({"type": "leave"})

    # ---- starting calls ----

    def call_finished(self, call):
        if self.call is call:
            self.call = None
        if self.pending is call:
            self.pending = None

    def _new_call(self, kind, meeting, reason=None):
        call = Call(self, kind, meeting, reason)
        self.call = call
        self.calls[call.id] = call
        while len(self.calls) > KEEP_CALLS:
            self.calls.pop(next(iter(self.calls)))
        return call

    def _busy(self):
        return web.HTTPConflict(text=json.dumps({"error": "already on a call", "call": self.call.id}),
                                content_type="application/json")

    async def start_call(self, kind, number, passcode, join_url="", reason=None, obf_token=""):
        """Start a call in a meeting. Raises web.HTTPException if it can't."""
        if self.call is not None:
            raise self._busy()
        m = self.meeting
        if m is not None and m.standby and not m.leaving and number == m.number:
            # The bot is already in the always-on meeting: use it.
            call = self._new_call(kind, m, reason)
            call.start_timer()
            call.maybe_greet()
            return call
        if not self.bridge_connected:
            raise web.HTTPServiceUnavailable(text=json.dumps({"error": "the Zoom bridge isn't connected"}),
                                             content_type="application/json")
        standby = bool(self.standby) and number == self.standby[0]
        meeting = Meeting(number, passcode, join_url or (self.standby[2] if standby else ""),
                          standby=standby, obf_token=obf_token)
        call = self._new_call(kind, meeting, reason)
        call.start_timer()
        if m is None:
            await self._send_join(meeting)
        else:
            # The bridge handles one meeting per run: leave this one; the call joins when it's back.
            self.pending = call
            await self.leave_meeting(m)
            log.info("[%s] leaving meeting %s first", call.id, m.number)
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
        if self.standby:
            number, passcode, join_url = self.standby
        elif self.zoom:
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
        call = await self.start_call("join", number, passcode, join_url, (body.get("reason") or "").strip() or None,
                                     obf_token=(body.get("obf_token") or "").strip())
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
            "always_on": {"join_url": self.standby[2],
                          "bot_in_meeting": bool(self.meeting and self.meeting.standby and self.meeting.in_meeting)}
            if self.standby else None,
        })

    async def healthz(self, request):
        return web.json_response({"ok": True, "bridge_connected": self.bridge_connected})

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
        if self.meeting is not None:
            await self.leave_meeting(self.meeting)
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
