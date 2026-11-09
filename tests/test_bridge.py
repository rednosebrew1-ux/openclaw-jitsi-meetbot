"""Bridge logic against a fake of the zoom_meeting_sdk bindings (the real SDK needs Linux x86_64 and Zoom)."""

import types

import jwt
import pytest

from bridge.bridge import ZoomBot, sdk_jwt

OK = 0


class Callbacks:
    """Stands in for the bindings' *Callbacks classes: keeps the Python callbacks it's given."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __getattr__(self, name):
        return self.kwargs[name]


class Recorder:
    """Any object whose methods we only need to record."""

    def __init__(self, zoom, name, returns=None):
        self._zoom, self._name, self._returns = zoom, name, returns or {}

    def __getattr__(self, method):
        def call(*args):
            self._zoom.calls.append((self._name, method, args))
            result = self._returns.get(method, OK)
            return result() if callable(result) else result
        return call


def make_zoom(can_record=True, join_result=OK):
    z = types.SimpleNamespace()
    z.calls = []
    z.SDKERR_SUCCESS, z.AUTHRET_SUCCESS = OK, OK
    (z.MEETING_STATUS_INMEETING, z.MEETING_STATUS_WAITINGFORHOST, z.MEETING_STATUS_IN_WAITING_ROOM,
     z.MEETING_STATUS_ENDED, z.MEETING_STATUS_FAILED) = range(10, 15)
    z.LEAVE_MEETING = "LEAVE"
    z.ZoomSDKAudioChannel_Mono = "MONO"
    z.SDK_LANGUAGE_ID = types.SimpleNamespace(LANGUAGE_English="en")
    z.SDKUserType = types.SimpleNamespace(SDK_UT_WITHOUT_LOGIN="anon")
    z.AudioRawdataSamplingRate = types.SimpleNamespace(AudioRawdataSamplingRate_32K="32k")
    z.state = {"can_record": can_record, "participants": [1]}  # 1 = the bot itself

    z.InitParam = types.SimpleNamespace
    z.InitSDK = lambda p: z.calls.append(("sdk", "InitSDK", (p,))) or OK
    z.CleanUPSDK = lambda: z.calls.append(("sdk", "CleanUPSDK", ()))
    for name in ("MeetingServiceEventCallbacks", "AuthServiceEventCallbacks", "MeetingReminderEventCallbacks",
                 "MeetingParticipantsCtrlEventCallbacks", "ZoomSDKVirtualAudioMicEventCallbacks",
                 "MeetingRecordingCtrlEventCallbacks", "ZoomSDKAudioRawDataDelegateCallbacks"):
        setattr(z, name, Callbacks)
    z.AuthContext = types.SimpleNamespace

    join_param = types.SimpleNamespace(param=types.SimpleNamespace())
    z.JoinParam = lambda: join_param
    z.join_param = join_param

    events = {}

    def set_event(kind):
        def setter(cb):
            events[kind] = cb
            return OK
        return setter

    myself = types.SimpleNamespace(GetUserID=lambda: 1)
    participants = Recorder(z, "participants", {
        "GetMySelfUser": myself, "GetParticipantsList": lambda: list(z.state["participants"])})
    participants.SetEvent = set_event("participants")
    recording = Recorder(z, "recording", {"CanStartRawRecording": lambda: OK if z.state["can_record"] else 12})
    recording.SetEvent = set_event("recording")
    reminder = Recorder(z, "reminder")
    reminder.SetEvent = set_event("reminder")
    audio = Recorder(z, "audio", {"CanUnMuteBySelf": True})
    meeting = Recorder(z, "meeting", {
        "Join": join_result, "GetMeetingParticipantsController": participants,
        "GetMeetingRecordingController": recording, "GetMeetingReminderController": reminder,
        "GetMeetingAudioController": audio})
    meeting.SetEvent = set_event("meeting")
    auth = Recorder(z, "auth")
    auth.SetEvent = set_event("auth")
    audio_settings = Recorder(z, "audio_settings")
    settings = Recorder(z, "settings", {"GetAudioSettings": audio_settings})
    helper = Recorder(z, "audio_helper")
    helper.setExternalAudioSource = lambda cb: events.__setitem__("mic", cb) or OK
    helper.subscribe = lambda cb, flag: events.__setitem__("audio", cb) or OK
    z.CreateMeetingService = lambda: meeting
    z.CreateSettingService = lambda: settings
    z.CreateAuthService = lambda: auth
    z.GetAudioRawdataHelper = lambda: helper
    z.DestroyMeetingService = z.DestroySettingService = z.DestroyAuthService = lambda s: None
    z.events = events
    return z


class Harness:
    def __init__(self, **zoom_kwargs):
        self.zoom = make_zoom(**zoom_kwargs)
        self.sent = []
        self.exits = []
        self.bot = ZoomBot(self.zoom, self.sent.append, self.exits.append)
        self.bot.init_sdk("client-id", "client-secret-for-tests-0123456789")

    def messages(self, kind=None):
        return [m for m in self.sent if isinstance(m, dict) and (kind is None or m["type"] == kind)]

    def called(self, obj, method):
        return [c for c in self.zoom.calls if c[0] == obj and c[1] == method]

    def login(self):
        self.zoom.events["auth"].onAuthenticationReturnCallback(OK)

    def enter_meeting(self):
        self.zoom.events["meeting"].onMeetingStatusChangedCallback(self.zoom.MEETING_STATUS_INMEETING, 0)


JOIN = {"type": "join", "meeting_number": "12345678901", "passcode": "abc", "display_name": "Max",
        "recording_token": "rec-token"}


def test_joins_after_login_with_the_right_settings():
    h = Harness()
    h.bot.handle(JOIN)
    assert not h.called("meeting", "Join")  # waits for the SDK login
    h.login()
    assert h.called("meeting", "Join")
    p = h.zoom.join_param.param
    assert (p.meetingNumber, p.psw, p.userName) == (12345678901, "abc", "Max")
    assert p.app_privilege_token == "rec-token"
    assert not hasattr(p, "onBehalfToken")  # only set when given
    assert p.isMyVoiceInMix is False and p.isAudioRawDataStereo is False
    assert p.eAudioRawdataSamplingRate == "32k" and h.zoom.join_param.userType == "anon"
    assert h.called("audio_settings", "EnableAutoJoinAudio") == [("audio_settings", "EnableAutoJoinAudio", (True,))]
    assert h.messages("status")[-1]["status"] == "joining"


def test_in_meeting_listens_and_relays_audio_both_ways():
    h = Harness()
    h.login()
    h.bot.handle(JOIN)
    h.enter_meeting()
    assert {"type": "status", "status": "in_meeting"} in h.sent
    assert h.called("audio", "JoinVoip")
    assert h.called("recording", "StartRawRecording")
    assert {"type": "recording", "allowed": True} in h.sent
    assert h.messages("participants") == [{"type": "participants", "others": 0}]

    # Someone joins, then their audio arrives.
    h.zoom.state["participants"] = [1, 7]
    h.zoom.events["participants"].onUserJoinCallback([7], "")
    assert h.messages("participants")[-1] == {"type": "participants", "others": 1}
    h.zoom.events["audio"].onMixedAudioRawDataReceivedCallback(types.SimpleNamespace(GetBuffer=lambda: b"\x01\x02"))
    assert b"\x01\x02" in h.sent

    # Nothing can be said until Zoom starts the virtual mic.
    sender = Recorder(h.zoom, "mic")
    h.zoom.events["mic"].onMicInitializeCallback(sender)
    h.bot.send_audio(b"early")
    assert not h.called("mic", "send")
    h.zoom.events["mic"].onMicStartSendCallback()
    assert {"type": "mic_ready"} in h.sent
    assert h.called("audio", "UnMuteAudio") == [("audio", "UnMuteAudio", (1,))]
    h.bot.send_audio(b"hello")
    assert h.called("mic", "send") == [("mic", "send", (b"hello", 32000, "MONO"))]


def test_asks_host_when_it_cant_record():
    h = Harness(can_record=False)
    h.login()
    h.bot.handle(JOIN)
    h.enter_meeting()
    assert h.called("recording", "RequestLocalRecordingPrivilege")
    assert not h.called("recording", "StartRawRecording")
    assert {"type": "recording", "allowed": False} in h.sent
    # The host clicks Allow.
    h.zoom.state["can_record"] = True
    h.zoom.events["recording"].onRecordPrivilegeChangedCallback(True)
    assert h.called("recording", "StartRawRecording")
    assert h.messages("recording")[-1] == {"type": "recording", "allowed": True}


def test_leave_and_end():
    h = Harness()
    h.login()
    h.bot.handle(JOIN)
    h.enter_meeting()
    h.bot.handle({"type": "leave"})
    assert h.called("meeting", "Leave") == [("meeting", "Leave", ("LEAVE",))]
    h.zoom.events["meeting"].onMeetingStatusChangedCallback(h.zoom.MEETING_STATUS_ENDED, 0)
    assert h.messages("ended") == [{"type": "ended", "failed": False, "reason": "left"}]
    assert h.exits == [0]


def test_join_failure_is_reported():
    h = Harness()
    h.login()
    h.bot.handle(JOIN)
    h.zoom.events["meeting"].onMeetingStatusChangedCallback(h.zoom.MEETING_STATUS_FAILED, 3)
    ended = h.messages("ended")[0]
    assert ended["failed"] is True and "error 3" in ended["reason"]
    assert h.exits == [0]


def test_join_rejected_right_away():
    h = Harness(join_result=5)
    h.login()
    h.bot.handle(JOIN)
    assert h.messages("ended")[0]["failed"] is True and h.exits == [0]


def test_bad_sdk_credentials_exit():
    h = Harness()
    h.zoom.events["auth"].onAuthenticationReturnCallback(2)
    assert h.messages("error") and h.exits == [1]


def test_second_join_is_refused():
    h = Harness()
    h.login()
    h.bot.handle(JOIN)
    h.bot.handle(JOIN)
    assert len(h.called("meeting", "Join")) == 1
    assert h.messages("error")[-1]["message"] == "already in a meeting"


def test_leave_when_idle_just_exits():
    h = Harness()
    h.bot.handle({"type": "leave"})
    assert h.exits == [0] and not h.called("meeting", "Leave")


def test_sdk_jwt():
    token = sdk_jwt("my-client", "my-secret-for-tests-0123456789abcdef", now=1_700_000_000)
    claims = jwt.decode(token, "my-secret-for-tests-0123456789abcdef", algorithms=["HS256"], options={"verify_exp": False})
    assert claims["appKey"] == "my-client"
    assert claims["iat"] == 1_700_000_000 - 30 and claims["tokenExp"] == claims["exp"] > claims["iat"]
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, "other-secret-for-tests-0123456789abc", algorithms=["HS256"], options={"verify_exp": False})


def test_passes_the_on_behalf_token():
    h = Harness()
    h.login()
    h.bot.handle(dict(JOIN, obf_token="obf-token"))
    assert h.zoom.join_param.param.onBehalfToken == "obf-token"
