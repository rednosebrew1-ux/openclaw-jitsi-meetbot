# OpenClaw Jitsi Meetbot

Talk with your AI by voice in a [Jitsi Meet](https://jitsi.org/jitsi-meet/) room on your own
server, in every direction:

- **Leave it on.** The bot waits in an always-on room. Join the link whenever you want to talk.
- **It calls you.** Your agent says "call me": you get a link (and a push notification, if you
  set one up). Tap it and talk.
- **You call it.** Ask it to join a room and it hops in.

The bot thinks with your [OpenClaw](https://openclaw.ai) agent through the Gateway's
OpenAI-compatible endpoint, so everything that agent can reach (your other agents, tools and
channels) is available on the call. Any other OpenAI-compatible model works too, such as Ollama.

Everything is free and open source and runs on your Mac, natively on Apple Silicon:

- Jitsi Meet itself, in Docker (official images).
- The bot, joining as a normal guest in headless Chromium.
- Speech: [faster-whisper](https://github.com/SYSTRAN/faster-whisper) for hearing,
  [Piper](https://github.com/rhasspy/piper) for speaking, and Silero VAD for turn-taking.
  You can interrupt it mid-sentence, and if your model is slow it says "One sec." instead of going quiet.

## How it works

```
 Jitsi room  <-- WebRTC -->  bridge                <-- WebSocket -->  voice server   <-- HTTP -->  OpenClaw
 (you)                       (headless Chromium)                      (turn-taking,                (your agent)
                                                                        Whisper, Piper)
```

- **jitsi_bridge/** opens the room in headless Chromium like any guest. Its microphone is the
  AI's voice, and everyone else's audio is mixed and sent to the voice server.
- **voice/** does the conversation, keeps the bot in the always-on room, and has a small API for
  starting calls.
- **skills/openclaw/jitsi-call** lets your OpenClaw agent start calls and read the transcripts.

## Setup on a Mac

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/) (for Jitsi) and Python 3.10 or newer.

### 1. Run Jitsi Meet

Follow the [docker-jitsi-meet quick start](https://jitsi.github.io/handbook/docs/devops-guide/devops-guide-docker/).
In short: download the latest docker-jitsi-meet release, then in its folder:

```bash
cp env.example .env
./gen-passwords.sh
mkdir -p ~/.jitsi-meet-cfg/{web,transcripts,prosody/config,prosody/prosody-plugins-custom,jicofo,jvb,jigasi,jibri}
docker compose up -d
```

Open https://localhost:8443 and accept the self-signed certificate. You now have Jitsi on your Mac.

To join from your phone too, make the Mac reachable from it (for example with
[Tailscale](https://tailscale.com)), set `PUBLIC_URL` in docker-jitsi-meet's `.env` to that
address, and set `JVB_ADVERTISE_IPS` to the Mac's IP on that network so audio can flow. Then
restart Jitsi. The docker-jitsi-meet guide covers this.

### 2. Configure the bot

```bash
git clone https://github.com/rednosebrew1-ux/openclaw-jitsi-meetbot.git
cd openclaw-jitsi-meetbot
cp .env.example .env
```

Edit `.env`:

- `API_TOKEN`: make one with `openssl rand -hex 24`.
- `JITSI_URL`: your Jitsi address, the same one you open in your browser.
- `ALWAYS_ON_MEETING_URL`: for example `https://localhost:8443/SomethingHardToGuess` (optional).
- The brain:

```bash
LLM_URL=http://127.0.0.1:18789/v1/chat/completions
LLM_API_KEY=<your OpenClaw Gateway token>
LLM_MODEL=openclaw:<your agent id>
```

If you haven't already, turn on the Gateway's endpoint with
`openclaw config set gateway.http.endpoints.chatCompletions.enabled true`, then restart the Gateway.

### 3. Start it

```bash
./start.sh
```

The first run sets up a Python environment, downloads Chromium, and downloads the Whisper and
Piper models. When you see `Jitsi bridge connected` (and, with an always-on room,
`in the always-on room ...`), open the room in your browser and say hi.

### 4. Add the OpenClaw skill

```bash
cp -r skills/openclaw/jitsi-call ~/.openclaw/skills/
mkdir -p ~/.config/openclaw-jitsi-meetbot
printf 'MEETBOT_TOKEN=%s\n' '<your API_TOKEN>' > ~/.config/openclaw-jitsi-meetbot/skill.env
chmod 600 ~/.config/openclaw-jitsi-meetbot/skill.env
```

Now tell your agent "call me", "join my Jitsi room", or "what's the link to talk to you?".

### Optional: a push notification when it calls

Install the free [ntfy](https://ntfy.sh) app, subscribe to a long random topic name, and set
`NOTIFY_URL=https://ntfy.sh/<that topic>`. When the AI calls, your phone buzzes; tap it to join.

## The always-on room

With `ALWAYS_ON_MEETING_URL` set, the bot sits in that room whenever it isn't on another call:

- Join the link any time and it says hello. Leave, and it stays and waits for next time.
- Each visit is its own conversation, with its own transcript.
- "Call me" sends you this same link instead of making a new room.
- If it's asked to join a different room, it steps out and comes back afterwards.
- If it gets dropped (Jitsi restarted, the Mac slept), it rejoins on its own.

Keep the room name hard to guess. Anyone who can reach your Jitsi server and knows the name can
talk to your agent, and through it use your tools. docker-jitsi-meet lets anyone create rooms by
default; see its docs for turning on authentication if your server is reachable beyond your own devices.

## API

All endpoints need `Authorization: Bearer <API_TOKEN>`.

| | |
|---|---|
| `POST /api/call` `{"reason": "..."}` | The AI calls you. Returns `id` and `join_url` right away. |
| `POST /api/join` `{"link": "..."}` | The AI joins a room: a link, a room name on `JITSI_URL`, or the always-on room if empty. |
| `GET /api/calls/{id}?wait=1` | Wait for the call to end; returns `status` and `transcript`. |
| `POST /api/calls/{id}/hangup` | End a call. |
| `GET /api/status` | Whether the bridge is connected, a call is going, and the always-on link. |

## Limits

- **One call at a time.** The bot is in one room at a time.
- **Tested against a fake Jitsi room, not a live Jitsi server yet.** The bridge runs in real
  Chromium in the tests, and audio goes over real WebRTC both ways. The fake room offers the
  same `APP.conference` calls as Jitsi Meet (the ones Jitsi's own test tools use). If a real
  server behaves differently, set `JITSI_HEADLESS=false` to watch the bot's browser, and check the bridge logs.

## Running on Linux with Docker

`docker compose up --build` runs the voice server and the bridge in containers. Point
`JITSI_URL` (or `JITSI_INTERNAL_URL`) at an address the containers can reach, and use
`host.docker.internal` in `LLM_URL` for an OpenClaw running on the same machine.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
python -m playwright install chromium
python -m pytest tests
```

MIT licensed. Not affiliated with 8x8 or the Jitsi project.
