# OpenClaw Zoom Bot

Talk with your AI on Zoom, in both directions:

- **It calls you.** Your agent (or you, from chat) says "call me". The bot opens a Zoom meeting,
  joins it, and sends you the link (and a push notification, if you set one up). Tap it and talk.
- **You call it.** Say "join my Zoom", or give it a meeting link. The bot joins and talks with you.

- **Or leave it on.** Give it one always-on meeting link: the bot waits in that meeting all the time,
  and you just join whenever you want to talk.

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
git clone https://github.com/rednosebrew1-ux/openclaw-zoom-bot.git
cd openclaw-zoom-bot
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
mkdir -p ~/.config/openclaw-zoom-bot
printf 'ZOOM_BOT_TOKEN=%s\n' '<your API_TOKEN>' > ~/.config/openclaw-zoom-bot/skill.env
chmod 600 ~/.config/openclaw-zoom-bot/skill.env
```

Now tell your agent "call me on Zoom" or "join my Zoom".

### Optional: an always-on meeting link

Set `ALWAYS_ON_MEETING_URL` to a meeting link that doesn't change, such as your personal meeting
link or a recurring meeting with no fixed time. In that meeting's settings, turn on **Allow
participants to join before host** and turn off the **waiting room**.

The bot then sits in that meeting whenever it isn't on another call:

- Join the link any time: it says hello. Leave: it stays and waits for the next time.
- Each visit is its own conversation, with its own transcript.
- "Call me" sends you this same link instead of making a new meeting.
- If it's asked to join a different meeting, it steps out and comes back afterwards.
- If Zoom ends the meeting, it rejoins within seconds. Free Zoom accounts end meetings after
  40 minutes, so a long conversation gets cut off there; paid accounts allow up to 30 hours.

Keep the link private: anyone who has it can talk to your agent, and through it use your tools.
Without the Zoom API settings, Zoom asks you to allow recording each time you join.

### Optional: a push notification when it calls

Install the free [ntfy](https://ntfy.sh) app, subscribe to a long random topic name, and set
`NOTIFY_URL=https://ntfy.sh/<that topic>`. When the AI calls, your phone buzzes; tap it to join.

### Optional: join automatically when your meeting starts

Set `ZOOM_WEBHOOK_SECRET` and `AUTO_JOIN_MEETINGS` (see `.env.example`). Then, in a Zoom app,
add an Event Subscription for **Meeting has started** pointing at `https://<your host>/zoom/webhook`.
This needs the voice server reachable from the internet, for example through a tunnel.

## Running the bridge on another computer

Everything can run on one Intel/AMD computer (Linux, Windows, or an Intel Mac). If yours is an
Apple Silicon Mac and the bridge misbehaves under emulation, run just the bridge on any x86_64
Linux machine (an old PC, a mini PC, or a small cloud server), and keep the rest on the Mac:

1. On the Mac, run everything as usual except the bridge: `docker compose up --build voice`.
2. On the x86_64 machine, clone this repo, copy the same `.env`, and add
   `VOICE_URL=ws://<the Mac's address>:8090/bridge`.
   Use a private network such as [Tailscale](https://tailscale.com) rather than the open internet.
3. Start only the bridge there: `docker compose up --build --no-deps bridge`.

## API

All endpoints need `Authorization: Bearer <API_TOKEN>`.

| | |
|---|---|
| `POST /api/call` `{"reason": "..."}` | The AI calls you. Returns `id` and `join_url` right away. |
| `POST /api/join` `{"link": "..."}` | The AI joins a meeting (your personal meeting if no link). |
| `GET /api/calls/{id}?wait=1` | Wait for the call to end; returns `status` and `transcript`. |
| `POST /api/calls/{id}/hangup` | Make the AI leave. |
| `GET /api/status` | Whether the bridge is connected, a call is going, and the always-on link. |

## Limits

- **One call at a time.** The Zoom SDK handles one meeting per bridge.
- **Other people's meetings need a token.** Since March 2026, Zoom requires Meeting SDK bots joining
  meetings outside the app's account to carry an "on behalf of" token. Put one in `ZOOM_OBF_TOKEN`,
  or pass `obf_token` to `/api/join`. This project doesn't fetch it for you yet, and it expires after
  about two hours.
- **The bridge needs an x86_64 (Intel/AMD) computer.** The Zoom Meeting SDK for Linux is built for
  x86_64. On an Apple Silicon Mac, Docker runs it under emulation, which Zoom says it doesn't support
  and which may be unreliable. See "Running the bridge on another computer" below.
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
