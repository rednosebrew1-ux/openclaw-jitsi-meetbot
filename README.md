# Zoom AI Caller

Talk with your AI on Zoom, in both directions:

- **It calls you.** Your agent (or you, from chat) says "call me". The bot opens a Zoom meeting,
  joins it, and sends you the link (and a push notification, if you set one up). Tap it and talk.
- **You call it.** Say "join my Zoom", or give it a meeting link. The bot joins and talks with you.

The bot thinks with your [OpenClaw](https://openclaw.ai) agent through the Gateway's
OpenAI-compatible endpoint. Everything that agent can reach (your other agents, tools and
channels) is available on the call. Any other OpenAI-compatible model works too, such as Ollama.

Speech is local and free by default: [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
for hearing, [Piper](https://github.com/rhasspy/piper) for speaking, and Silero VAD for
turn-taking. You can interrupt it mid-sentence, and if your model is slow it says "One sec."
instead of going quiet.

## How it works

```
 Zoom meeting  <-- Zoom Meeting SDK -->  bridge  <-- WebSocket -->  voice server  <-- HTTP -->  OpenClaw
 (you)                                   (Docker, linux/amd64)      (turn-taking,               (your agent)
                                                                     Whisper, Piper)
```

- **bridge/** joins one meeting with the official Zoom Meeting SDK for Linux (through the
  [py-zoom-meeting-sdk](https://github.com/noah-duncan/py-zoom-meeting-sdk) bindings). It passes
  the meeting's audio to the voice server and plays the replies through a virtual microphone.
  The SDK handles one meeting per process, so the bridge restarts after each call.
- **voice/** does the conversation, and has a small API for starting calls.
- **skills/openclaw/zoom-call** lets your OpenClaw agent start calls and read the transcripts.

## Setup

You need Docker (Docker Desktop on a Mac) and a Zoom account.

### 1. Make a Zoom app for the Meeting SDK

1. Go to [marketplace.zoom.us](https://marketplace.zoom.us/), sign in, then **Develop > Build App > General App**.
2. Under **Features > Embed**, turn on **Meeting SDK**.
3. Copy the **Client ID** and **Client Secret** from **App Credentials**.

You don't need to publish the app: it works for meetings on your own Zoom account.

### 2. Pick how the bot gets a meeting

Choose one:

- **Your personal meeting (simplest).** Copy your personal meeting link (the one with
  `/j/<number>?pwd=...`). In its settings, turn on **Allow participants to join before host**.
  Each call, Zoom asks you (the host) whether to **allow the bot to record**. Click Allow:
  the bot can't hear you until you do, and it will say so.
- **The Zoom API (smoother).** Make a **Server-to-Server OAuth** app as well (**Develop > Build App >
  Server-to-Server OAuth**). Give it scopes to create meetings, read meetings, and read a
  meeting's local recording join token, then activate it. The bot then makes a fresh meeting for
  every call and listens without the recording prompt.

### 3. Configure and start

```bash
git clone https://github.com/rednosebrew1-ux/zoom-ai-caller.git
cd zoom-ai-caller
cp .env.example .env
```

Edit `.env`. At least: `API_TOKEN` (make one with `openssl rand -hex 24`), `ZOOM_SDK_CLIENT_ID`,
`ZOOM_SDK_CLIENT_SECRET`, `PERSONAL_MEETING_URL` or the three `ZOOM_ACCOUNT_ID`/`ZOOM_CLIENT_*`
settings, and the brain:

```bash
LLM_URL=http://host.docker.internal:18789/v1/chat/completions
LLM_API_KEY=<your OpenClaw Gateway token>
LLM_MODEL=openclaw:<your agent id>
```

If you haven't already, turn on the Gateway's endpoint with
`openclaw config set gateway.http.endpoints.chatCompletions.enabled true`, then restart the Gateway.

Start it:

```bash
docker compose up --build
```

The first start downloads the Whisper and Piper models into `./data`. When you see
`Zoom bridge connected` and `Zoom bridge ready`, it's working.

### 4. Add the OpenClaw skill

```bash
cp -r skills/openclaw/zoom-call ~/.openclaw/skills/
mkdir -p ~/.config/zoom-ai-caller
printf 'ZOOM_CALLER_TOKEN=%s\n' '<your API_TOKEN>' > ~/.config/zoom-ai-caller/skill.env
chmod 600 ~/.config/zoom-ai-caller/skill.env
```

Now tell your agent "call me on Zoom" or "join my Zoom".

### Optional: a push notification when it calls

Install the free [ntfy](https://ntfy.sh) app, subscribe to a long random topic name, and set
`NOTIFY_URL=https://ntfy.sh/<that topic>`. When the AI calls, your phone buzzes; tap it to join.

### Optional: join automatically when your meeting starts

Set `ZOOM_WEBHOOK_SECRET` and `AUTO_JOIN_MEETINGS` (see `.env.example`). Then, in a Zoom app,
add an Event Subscription for **Meeting has started** pointing at `https://<your host>/zoom/webhook`.
This needs the voice server reachable from the internet, for example through a tunnel.

## API

All endpoints need `Authorization: Bearer <API_TOKEN>`.

| | |
|---|---|
| `POST /api/call` `{"reason": "..."}` | The AI calls you. Returns `id` and `join_url` right away. |
| `POST /api/join` `{"link": "..."}` | The AI joins a meeting (your personal meeting if no link). |
| `GET /api/calls/{id}?wait=1` | Wait for the call to end; returns `status` and `transcript`. |
| `POST /api/calls/{id}/hangup` | Make the AI leave. |
| `GET /api/status` | Whether the bridge is connected and a call is going. |

## Limits

- **One call at a time.** The Zoom SDK handles one meeting per bridge.
- **Your own meetings only.** Since March 2026, Zoom requires Meeting SDK bots joining meetings
  outside the app's account to carry an "on behalf of" token, which this project doesn't do.
- **Apple Silicon Macs** run the bridge under emulation (the SDK is x86_64 only). It only moves
  audio, so that's fine; the voice server runs natively.
- **Not yet tested against a live Zoom meeting.** The voice server and the bridge's logic are
  covered by tests with fakes (`python -m pytest tests`), and the bridge uses the same SDK calls
  as the bindings' sample program. If something goes wrong in a real meeting, the bridge logs say
  which step failed.

## Development

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt aiohttp pyjwt pytest
python -m pytest tests
```

You can also run the voice server without Docker: `python -m voice` reads `.env` (use
`LLM_URL=http://127.0.0.1:18789/...` there), and set the bridge's `VOICE_URL` in
`docker-compose.yml` to `ws://host.docker.internal:8090/bridge`.

Not affiliated with or endorsed by Zoom Video Communications. MIT licensed.
