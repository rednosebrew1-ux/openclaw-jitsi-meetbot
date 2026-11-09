"""Zoom bridge: joins one Zoom meeting with the Zoom Meeting SDK and relays its audio.

Meeting audio (everyone but the bot, mixed, 32 kHz 16-bit mono) goes to the voice server over a
WebSocket; audio the voice server sends back is played into the meeting through a virtual mic.

The SDK handles one meeting per process, so the bridge exits after each meeting and Docker
restarts it (restart: unless-stopped). Run inside the Docker image: python3 bridge.py
"""

import asyncio
import json
import logging
import os
import sys
import threading
import time

import aiohttp
import jwt

log = logging.getLogger("bridge")

RATE = 32000


def sdk_jwt(client_id, client_secret, now=None):
    """The token the Meeting SDK logs in with (signed with your Meeting SDK app's secret)."""
    now = int(now or time.time())
    iat = now - 30  # allow for a little clock skew
    exp = iat + 2 * 3600
    payload = {"appKey": client_id, "iat": iat, "exp": exp, "tokenExp": exp}
    return jwt.encode(payload, client_secret, algorithm="HS256")


class ZoomBot:
    """Drives the SDK. Every method runs on the GLib main thread (use `schedule` from elsewhere)."""

    def __init__(self, zoom, emit, exit_process):
        self.zoom = zoom
        self.emit = emit                   # thread-safe: dict -> JSON message, bytes -> audio
        self.exit_process = exit_process
        self.authed = False
        self.pending_join = None
        self.in_meeting = False
        self.joining = False
        self.leaving = False
        self.my_id = None
        self.mic_sender = None
        self.mic_started = False
        self.recording = False
        self._others = None
        # Keep every callback object alive: the SDK holds raw pointers to them.
        self._keep = []
        self.meeting_service = self.setting_service = self.auth_service = None
        self.participants_ctrl = self.audio_ctrl = self.recording_ctrl = None
        self.audio_helper = None

    # ---- startup ----

    def init_sdk(self, client_id, client_secret):
        z = self.zoom
        param = z.InitParam()
        param.strWebDomain = "https://zoom.us"
        param.strSupportUrl = "https://zoom.us"
        param.enableGenerateDump = False
        param.emLanguageID = z.SDK_LANGUAGE_ID.LANGUAGE_English
        param.enableLogByDefault = False
        if z.InitSDK(param) != z.SDKERR_SUCCESS:
            raise RuntimeError("the Zoom SDK failed to start")

        self.meeting_service = z.CreateMeetingService()
        self.setting_service = z.CreateSettingService()
        meeting_events = z.MeetingServiceEventCallbacks(onMeetingStatusChangedCallback=self.on_status)
        self._keep.append(meeting_events)
        if self.meeting_service.SetEvent(meeting_events) != z.SDKERR_SUCCESS:
            raise RuntimeError("the Zoom SDK's meeting service didn't start")

        self.auth_service = z.CreateAuthService()
        auth_events = z.AuthServiceEventCallbacks(onAuthenticationReturnCallback=self.on_auth)
        self._keep.append(auth_events)
        self.auth_service.SetEvent(auth_events)
        context = z.AuthContext()
        context.jwt_token = sdk_jwt(client_id, client_secret)
        result = self.auth_service.SDKAuth(context)
        if result != z.SDKERR_SUCCESS:
            raise RuntimeError(f"Zoom SDK login didn't start (error {result})")

    def on_auth(self, result):
        if result != self.zoom.AUTHRET_SUCCESS:
            log.error("Zoom SDK login failed (result %s). Check ZOOM_SDK_CLIENT_ID and ZOOM_SDK_CLIENT_SECRET "
                      "(from a Zoom app with the Meeting SDK turned on).", result)
            self.emit({"type": "error", "message": f"Zoom SDK login failed (result {result})"})
            self.exit_process(1)
            return
        log.info("logged in to the Zoom SDK")
        self.authed = True
        if self.pending_join:
            self.join(self.pending_join)

    # ---- commands from the voice server ----

    def handle(self, message):
        kind = message.get("type")
        if kind == "join":
            self.join(message)
        elif kind == "leave":
            self.leave()

    def join(self, message):
        if self.in_meeting or self.joining:
            self.emit({"type": "error", "message": "already in a meeting"})
            return
        if not self.authed:
            self.pending_join = message  # joins as soon as the SDK login finishes
            return
        self.pending_join = None
        z = self.zoom
        join_param = z.JoinParam()
        join_param.userType = z.SDKUserType.SDK_UT_WITHOUT_LOGIN
        p = join_param.param
        p.meetingNumber = int(message["meeting_number"])
        p.userName = message.get("display_name") or "Assistant"
        p.psw = message.get("passcode") or ""
        p.isVideoOff = True
        p.isAudioOff = False
        p.isAudioRawDataStereo = False
        p.isMyVoiceInMix = False
        p.eAudioRawdataSamplingRate = z.AudioRawdataSamplingRate.AudioRawdataSamplingRate_32K
        if message.get("recording_token"):
            # Lets the bot hear the meeting without asking the host for recording permission.
            p.app_privilege_token = message["recording_token"]
        if message.get("obf_token"):
            # "On behalf of" token: required by Zoom to join meetings hosted outside your account.
            p.onBehalfToken = message["obf_token"]
        result = self.meeting_service.Join(join_param)
        if result != z.SDKERR_SUCCESS:
            self.emit({"type": "ended", "failed": True, "reason": f"couldn't join (error {result})"})
            self.exit_process(0)
            return
        self.joining = True
        self.emit({"type": "status", "status": "joining"})
        self.setting_service.GetAudioSettings().EnableAutoJoinAudio(True)
        log.info("joining meeting %s", p.meetingNumber)

    def leave(self):
        if self.leaving:
            return
        self.leaving = True
        if self.meeting_service and (self.in_meeting or self.joining):
            log.info("leaving the meeting")
            self.meeting_service.Leave(self.zoom.LEAVE_MEETING)
        else:
            self.exit_process(0)

    def send_audio(self, pcm):
        if self.mic_sender is None or not self.mic_started:
            return
        result = self.mic_sender.send(pcm, RATE, self.zoom.ZoomSDKAudioChannel_Mono)
        if result != self.zoom.SDKERR_SUCCESS:
            log.debug("sending audio failed: %s", result)

    # ---- meeting events ----

    def on_status(self, status, result=0):
        z = self.zoom
        if status == z.MEETING_STATUS_INMEETING:
            self.joining, self.in_meeting = False, True
            self.emit({"type": "status", "status": "in_meeting"})
            self.on_join()
        elif status == z.MEETING_STATUS_WAITINGFORHOST:
            self.emit({"type": "status", "status": "waiting_for_host"})
        elif status == z.MEETING_STATUS_IN_WAITING_ROOM:
            self.emit({"type": "status", "status": "in_waiting_room"})
        elif status == z.MEETING_STATUS_ENDED:
            self._ended(False, "the meeting ended" if not self.leaving else "left")
        elif status == z.MEETING_STATUS_FAILED:
            self._ended(True, f"couldn't join the meeting (Zoom error {result})")

    def _ended(self, failed, reason):
        log.info("meeting over: %s", reason)
        self.in_meeting = self.joining = False
        self.emit({"type": "ended", "failed": failed, "reason": reason})
        self.exit_process(0)

    def on_join(self):
        z = self.zoom
        reminder_events = z.MeetingReminderEventCallbacks(onReminderNotifyCallback=self.on_reminder)
        self._keep.append(reminder_events)
        self.meeting_service.GetMeetingReminderController().SetEvent(reminder_events)

        self.participants_ctrl = self.meeting_service.GetMeetingParticipantsController()
        participant_events = z.MeetingParticipantsCtrlEventCallbacks(
            onUserJoinCallback=self.on_people_changed, onUserLeftCallback=self.on_people_changed)
        self._keep.append(participant_events)
        self.participants_ctrl.SetEvent(participant_events)
        self.my_id = self.participants_ctrl.GetMySelfUser().GetUserID()

        # Raw audio needs the bot on computer audio (a workaround for SDK versions after 6.3.5).
        self.audio_ctrl = self.meeting_service.GetMeetingAudioController()
        self.audio_ctrl.JoinVoip()

        # The virtual mic: what the AI says goes in here.
        self.audio_helper = z.GetAudioRawdataHelper()
        mic_events = z.ZoomSDKVirtualAudioMicEventCallbacks(
            onMicInitializeCallback=self.on_mic_initialize, onMicStartSendCallback=self.on_mic_start,
            onMicStopSendCallback=self.on_mic_stop)
        self._keep.append(mic_events)
        result = self.audio_helper.setExternalAudioSource(mic_events)
        if result != z.SDKERR_SUCCESS:
            self.emit({"type": "error", "message": f"couldn't set up the bot's microphone (error {result})"})

        self.recording_ctrl = self.meeting_service.GetMeetingRecordingController()
        recording_events = z.MeetingRecordingCtrlEventCallbacks(
            onRecordPrivilegeChangedCallback=self.on_recording_privilege)
        self._keep.append(recording_events)
        self.recording_ctrl.SetEvent(recording_events)
        self.start_listening()
        self.on_people_changed()

    def on_reminder(self, content, handler):
        # Notices such as "this meeting is being recorded": accept so the bot isn't stuck.
        if handler:
            handler.Accept()

    def on_people_changed(self, *args):
        if not self.participants_ctrl:
            return
        ids = list(self.participants_ctrl.GetParticipantsList() or [])
        others = len([i for i in ids if i != self.my_id])
        if others != self._others:
            self._others = others
            self.emit({"type": "participants", "others": others})

    def start_listening(self):
        """Hear the meeting. Without a recording token, the host is asked to allow it."""
        z = self.zoom
        if self.recording:
            return
        if self.recording_ctrl.CanStartRawRecording() != z.SDKERR_SUCCESS:
            log.info("asking the host for permission to hear the meeting")
            self.recording_ctrl.RequestLocalRecordingPrivilege()
            self.emit({"type": "recording", "allowed": False})
            return
        result = self.recording_ctrl.StartRawRecording()
        if result != z.SDKERR_SUCCESS:
            self.emit({"type": "recording", "allowed": False})
            self.emit({"type": "error", "message": f"couldn't start listening (error {result})"})
            return
        audio_events = z.ZoomSDKAudioRawDataDelegateCallbacks(
            onMixedAudioRawDataReceivedCallback=self.on_mixed_audio, collectPerformanceData=False)
        self._keep.append(audio_events)
        result = self.audio_helper.subscribe(audio_events, False)
        if result != z.SDKERR_SUCCESS:
            self.emit({"type": "error", "message": f"couldn't subscribe to meeting audio (error {result})"})
            return
        self.recording = True
        log.info("listening to the meeting")
        self.emit({"type": "recording", "allowed": True})

    def on_recording_privilege(self, can_record):
        if can_record:
            self.start_listening()
        else:
            self.recording = False
            self.emit({"type": "recording", "allowed": False})

    def on_mic_initialize(self, sender):
        self.mic_sender = sender

    def on_mic_start(self):
        self.mic_started = True
        if self.audio_ctrl is not None and self.my_id is not None and self.audio_ctrl.CanUnMuteBySelf():
            self.audio_ctrl.UnMuteAudio(self.my_id)
        self.emit({"type": "mic_ready"})

    def on_mic_stop(self):
        self.mic_started = False

    def on_mixed_audio(self, data):
        self.emit(data.GetBuffer())

    def cleanup(self):
        z = self.zoom
        if self.audio_helper and self.recording:
            self.audio_helper.unSubscribe()
        for destroy, service in ((z.DestroyMeetingService, self.meeting_service),
                                 (z.DestroySettingService, self.setting_service),
                                 (z.DestroyAuthService, self.auth_service)):
            if service:
                destroy(service)
        z.CleanUPSDK()


class Link:
    """The WebSocket to the voice server, on its own thread with its own asyncio loop."""

    def __init__(self, url, token, on_message, on_audio, on_disconnect):
        self.url, self.token = url, token
        self.on_message, self.on_audio, self.on_disconnect = on_message, on_audio, on_disconnect
        self.loop = asyncio.new_event_loop()
        self._outbox = None
        self.connected = threading.Event()

    def start(self):
        threading.Thread(target=self._thread, name="voice-link", daemon=True).start()

    def send(self, message):
        """Thread-safe. Dropped while disconnected."""
        if self._outbox is not None and self.connected.is_set():
            self.loop.call_soon_threadsafe(self._outbox.put_nowait, message)

    def flush(self, timeout=1.0):
        """Wait briefly for queued messages to go out (before the process exits)."""
        end = time.monotonic() + timeout
        while self._outbox is not None and not self._outbox.empty() and time.monotonic() < end:
            time.sleep(0.02)

    def _thread(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._run())

    async def _run(self):
        self._outbox = asyncio.Queue()
        delay = 1
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(self.url, heartbeat=20,
                                                  headers={"Authorization": f"Bearer {self.token}"}) as ws:
                        log.info("connected to the voice server at %s", self.url)
                        delay = 1
                        self.connected.set()
                        await ws.send_json({"type": "hello"})
                        sender = asyncio.ensure_future(self._pump(ws))
                        try:
                            async for msg in ws:
                                if msg.type == aiohttp.WSMsgType.BINARY:
                                    self.on_audio(msg.data)
                                elif msg.type == aiohttp.WSMsgType.TEXT:
                                    try:
                                        self.on_message(json.loads(msg.data))
                                    except json.JSONDecodeError:
                                        pass
                        finally:
                            sender.cancel()
                            self.connected.clear()
                        self.on_disconnect()
            except (aiohttp.ClientError, OSError) as e:
                self.connected.clear()
                log.warning("can't reach the voice server at %s (%s); retrying in %ss", self.url, e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def _pump(self, ws):
        while True:
            message = await self._outbox.get()
            if isinstance(message, bytes):
                await ws.send_bytes(message)
            else:
                await ws.send_json(message)


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    missing = [n for n in ("ZOOM_SDK_CLIENT_ID", "ZOOM_SDK_CLIENT_SECRET", "API_TOKEN") if not os.environ.get(n)]
    if missing:
        log.error("missing settings: %s (see .env.example)", ", ".join(missing))
        sys.exit(1)

    import gi
    gi.require_version("GLib", "2.0")
    from gi.repository import GLib
    import zoom_meeting_sdk as zoom

    def schedule(fn, *args):
        def run():
            fn(*args)
            return False
        GLib.idle_add(run)

    link = None
    bot = None

    def exit_process(code=0):
        def finish():
            if link:
                link.flush()
            try:
                bot.cleanup()
            except Exception as e:  # never block the exit on SDK teardown
                log.debug("cleanup failed: %r", e)
            os._exit(code)
        GLib.timeout_add(300, lambda: (finish(), False)[1])

    def on_disconnect():
        # The voice server lost track of this call: don't sit in the meeting silently.
        schedule(lambda: bot.leave() if (bot.in_meeting or bot.joining) else None)

    url = os.environ.get("VOICE_URL", "ws://voice:8090/bridge")
    link = Link(url, os.environ["API_TOKEN"],
                on_message=lambda m: schedule(bot.handle, m),
                on_audio=lambda pcm: schedule(bot.send_audio, pcm),
                on_disconnect=on_disconnect)
    bot = ZoomBot(zoom, link.send, exit_process)
    bot.init_sdk(os.environ["ZOOM_SDK_CLIENT_ID"], os.environ["ZOOM_SDK_CLIENT_SECRET"])
    link.start()
    log.info("Zoom bridge ready")
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
