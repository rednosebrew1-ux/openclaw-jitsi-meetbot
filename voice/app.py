"""HTTP API, the bridge connection, and the life of each call in a Jitsi room."""

import asyncio
import hmac
import json
import logging
import re
import secrets
import time

import aiohttp
from aiohttp import web

from .audio import Speaker
from .conversation import Conversation
from .rooms import jitsi_room

log = logging.getLogger(__name__)

KEEP_CALLS = 50


class Meeting:
    """The room the bridge is in (or joining). In always-on mode it outlives many calls."""

    def __init__(self, url, standby=False):
        self.url = url
        self.standby = standby              # the always-on room: the bot stays when people leave
        self.in_meeting = False
        self.mic_ready = False
        self.hearing = False                # the bridge hears the room
        self.others = 0                     # people in the room besides the bot
        self.leaving = False
        # In always-on mode, a new call starts when someone arrives. After a call ends while people
        # are still there (the AI said goodbye), wait until the room empties before starting another.
        self.call_blocked = False


class Call:
    """One conversation in a room, from the bot joining (or someone arriving) to the end."""

    GREET_DELAY = 1.5  # seconds after someone joins, so their audio is connected before we talk

    def __init__(self, app, kind, meeting, reason=None):
        self.app = app
        self.id = secrets.token_hex(6)
        self.kind = kind                    # "ai_call" (the AI called you), "join" (you called it),
                                            # or "standby" (you joined the always-on room)
        self.meeting = meeting
        self.reason = reason
        self.status = "waiting" if meeting.in_meeting else "joining"
        self.error = None
        self.created = time.time()
        self.ended_at = None
        self.done = asyncio.Event()
        self.present = meeting.in_meeting and meeting.others > 0
        self.greeted = False
        self._timer = None
        self._greet_task = None
        self.speaker = Speaker(app.send_audio)
        self.speaker.start()
        self.conversation = Conversation(app.cfg, app.stt, app.tts, self.speaker, self.id,
                                         reason=reason, on_hangup=self.hang_up)

    @property
    def join_url(self):
        return self.meeting.url

    def info(self):
        return {
            "id": self.id, "kind": self.kind, "status": self.status, "join_url": self.join_url,
            "always_on": self.meeting.standby, "reason": self.reason, "error": self.error,
            "transcript": self.conversation.transcript,
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
            if self.meeting.others == 0 and self.present:
                log.info("[%s] everyone else left", self.id)
                await self.finish("completed" if self.greeted else "missed", leave=True)
                return
            self.present = self.meeting.others > 0
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
        if self.finished or self.greeted or self._greet_task or not (self.present and m.mic_ready and m.hearing):
            return
        self._greet_task = asyncio.ensure_future(self._greet())

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
        """End this conversation. `leave` makes the bot leave too, except in the always-on room."""
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


class MeetBotApp:
    def __init__(self, cfg, stt, tts):
        self.cfg, self.stt, self.tts = cfg, stt, tts
        self.bridge = None          # the bridge's WebSocket
        self.meeting = None         # the room the bridge is in or joining
        self.call = None            # the active call, if any
        self.pending = None         # a call waiting for the bridge to be free before it can join
        self.calls = {}             # recent calls by id
        self.standby_failures = 0
        self._tasks = set()
        self.standby = self.resolve(cfg.always_on_meeting_url) if cfg.always_on_meeting_url else None

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
        log.info("Jitsi bridge connected")
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
            log.info("Jitsi bridge disconnected")
            self.meeting = None
            call = self.call
            if call is not None and call is not self.pending:
                call.error = call.error or "the Jitsi bridge disconnected"
                await call.finish("completed" if call.greeted else "failed")
        return ws

    async def _on_meeting_event(self, event):
        m = self.meeting
        if m is None:
            return
        kind = event.get("type")
        if kind == "status":
            log.info("room: %s", event.get("status"))
            if event.get("status") == "in_meeting":
                m.in_meeting = True
                if m.standby:
                    self.standby_failures = 0
                    log.info("in the always-on room %s; join it any time to talk", m.url)
        elif kind == "mic_ready":
            m.mic_ready = True
        elif kind == "recording":
            m.hearing = bool(event.get("allowed"))
        elif kind == "participants":
            m.others = int(event.get("others") or 0)
            if m.others == 0:
                m.call_blocked = False
        elif kind == "ended":
            log.info("bot left the room (%s)", event.get("reason") or "ended")
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
            # Someone joined the always-on room: start a conversation.
            log.info("someone joined the always-on room")
            self._new_call("standby", m).maybe_greet()

    async def _bridge_ready(self):
        """The bridge is connected and free: send any waiting call, or go back to the always-on room."""
        if self.pending is not None:
            call, self.pending = self.pending, None
            if not call.finished:
                await self._send_join(call.meeting)
            return
        if not self.standby or self.meeting is not None or self.call is not None:
            return
        if self.standby_failures:
            delay = min(300, 5 * 2 ** (self.standby_failures - 1))
            log.info("rejoining the always-on room in %ss", delay)
            await asyncio.sleep(delay)
            if self.meeting is not None or self.call is not None or not self.bridge_connected:
                return
        await self._send_join(Meeting(self.standby, standby=True))

    async def _send_join(self, meeting):
        self.meeting = meeting
        await self.send_bridge({"type": "join", "url": meeting.url, "display_name": self.cfg.ai_name})
        log.info("joining %s%s", meeting.url, " (always on)" if meeting.standby else "")

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

    async def start_call(self, kind, url, reason=None):
        """Start a call in a room. Raises web.HTTPException if it can't."""
        if self.call is not None:
            raise web.HTTPConflict(text=json.dumps({"error": "already on a call", "call": self.call.id}),
                                   content_type="application/json")
        m = self.meeting
        if m is not None and m.standby and not m.leaving and url == m.url:
            # The bot is already in the always-on room: use it.
            call = self._new_call(kind, m, reason)
            call.start_timer()
            call.maybe_greet()
            return call
        if not self.bridge_connected:
            raise web.HTTPServiceUnavailable(text=json.dumps({"error": "the Jitsi bridge isn't connected"}),
                                             content_type="application/json")
        call = self._new_call(kind, Meeting(url, standby=url == self.standby), reason)
        call.start_timer()
        if m is None:
            await self._send_join(call.meeting)
        else:
            # One room at a time: leave this one; the call joins when the bridge is free.
            self.pending = call
            await self.leave_meeting(m)
            log.info("[%s] leaving %s first", call.id, m.url)
        return call

    def resolve(self, link):
        """A room link or room name as a normalized room URL."""
        return jitsi_room(link, self.cfg.jitsi_url)

    def _new_room(self):
        name = re.sub(r"[^A-Za-z0-9]+", "", self.cfg.ai_name) or "Assistant"
        # Hard to guess: anyone who has the link can talk to your agent.
        return self.resolve(f"{self.cfg.jitsi_url}/{name}{secrets.token_hex(6)}")

    async def call_user(self, request):
        """The AI calls you: it joins a room and sends you the link."""
        self._require_auth(request)
        body = await self._json(request)
        reason = (body.get("reason") or "").strip() or None
        if self.standby:
            url = self.standby
        elif self.cfg.jitsi_url:
            url = self._new_room()
        else:
            return self._error(400, "set JITSI_URL (your Jitsi server) or ALWAYS_ON_MEETING_URL, "
                                    "so the bot has a room to call you from")
        call = await self.start_call("ai_call", url, reason)
        self._spawn(self._notify(call))
        return web.json_response(call.info())

    async def join(self, request):
        """You called it: the bot joins your room (the always-on room if no link is given)."""
        self._require_auth(request)
        body = await self._json(request)
        link = (body.get("link") or "").strip()
        try:
            if link:
                url = self.resolve(link)
            elif self.standby:
                url = self.standby
            else:
                return self._error(400, "no room given, and ALWAYS_ON_MEETING_URL isn't set")
        except ValueError as e:
            return self._error(400, str(e))
        call = await self.start_call("join", url, (body.get("reason") or "").strip() or None)
        return web.json_response(call.info())

    async def _notify(self, call):
        if not self.cfg.notify_url:
            return
        text = f"{self.cfg.ai_name} is calling you. Tap to join: {call.join_url}"
        headers = {"Title": f"Call from {self.cfg.ai_name}", "Click": call.join_url, "Priority": "high",
                   "Tags": "telephone_receiver"}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.post(self.cfg.notify_url, data=text.encode(), headers=headers) as r:
                    if r.status >= 400:
                        log.warning("notification failed: %s %s", r.status, (await r.text())[:200])
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("notification failed: %r", e)

    # ---- call status ----

    def _find(self, request):
        call = self.calls.get(request.match_info["id"])
        if call is None:
            raise web.HTTPNotFound(text=json.dumps({"error": "no such call"}), content_type="application/json")
        return call

    async def get_call(self, request):
        self._require_auth(request)
        call = self._find(request)
        if request.query.get("wait") and not call.finished:
            timeout = min(float(request.query.get("timeout", 600)), 3600)
            try:
                await asyncio.wait_for(call.done.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        return web.json_response(call.info())

    async def hangup_call(self, request):
        self._require_auth(request)
        call = self._find(request)
        await call.finish("completed" if call.greeted else "missed", leave=True)
        return web.json_response(call.info())

    async def status(self, request):
        self._require_auth(request)
        return web.json_response({
            "bridge_connected": self.bridge_connected,
            "call": self.call.info() if self.call else None,
            "always_on": {"join_url": self.standby,
                          "bot_in_room": bool(self.meeting and self.meeting.standby and self.meeting.in_meeting)}
            if self.standby else None,
        })

    async def healthz(self, request):
        return web.json_response({"ok": True, "bridge_connected": self.bridge_connected})

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
        app.on_shutdown.append(self.on_shutdown)
        return app
