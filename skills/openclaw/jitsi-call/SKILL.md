---
name: jitsi-call
description: Talk with the user by voice in a Jitsi Meet room. Use when the user asks you to call them, to join their Jitsi room or "hop on a call", asks for the link to talk to you, or when something is urgent or easier to discuss by voice and the user has said calls are OK.
---

# Jitsi call

A voice bot (the user's self-hosted openclaw-jitsi-meetbot) joins a room on the user's Jitsi
Meet server and talks with them. It thinks with this same OpenClaw agent, so on the call it can
do anything you can. You get the transcript when the call ends.

## Setup (once)

Put the server's `API_TOKEN` in a private file the script reads:

```bash
mkdir -p ~/.config/openclaw-jitsi-meetbot
printf 'MEETBOT_TOKEN=%s\n' 'PASTE_API_TOKEN' > ~/.config/openclaw-jitsi-meetbot/skill.env
chmod 600 ~/.config/openclaw-jitsi-meetbot/skill.env
```

Add `MEETBOT_URL=http://...` to the same file if the server isn't at `http://localhost:8090`.

## Always-on room

If the server has an always-on room, the bot is always waiting there and the user can just
join it. `call.sh status` shows the link as `always_on.join_url`; give it to the user when they
ask how to reach you. Each visit to that room is its own call.

## Call the user

```bash
{baseDir}/scripts/call.sh call "Short reason for the call, written as context for the voice bot"
```

This returns right away with JSON including `id` and `join_url`. **Send the user the
`join_url` in this chat** so they can tap it and join (they may also get a push notification,
if the server has one set up). Then wait:

```bash
{baseDir}/scripts/call.sh wait ID
```

## Join a room the user names

```bash
{baseDir}/scripts/call.sh join "https://meet.example.com/SomeRoom"   # a link
{baseDir}/scripts/call.sh join SomeRoom                            # a room on their server
```

Then `call.sh wait ID` as above.

## Results

`wait` prints JSON when the call ends:

- `status`: `completed`, `missed` (nobody joined in time), or `failed` (see `error`)
- `transcript`: list of `{role, text}` turns (`assistant` = the voice bot, `user` = the person)

`call.sh hangup ID` ends a call. `call.sh status` shows whether the bridge is connected and
whether a call is already going (only one at a time).

## Rules

- Only call when the user asked to be called, or has said it's OK to call for this kind of thing.
- Put everything the voice bot needs in the reason: what happened, what to ask, what decision
  is needed. It doesn't see this chat.
- If the status is `missed`, don't call again right away. Send a message instead.
- If it fails with "the Jitsi bridge isn't connected", tell the user the meetbot isn't running
  (they start it with `./start.sh`).
- After a completed call, act on what the user said in the transcript and confirm in chat
  what you did.
