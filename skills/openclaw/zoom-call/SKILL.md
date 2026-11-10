---
name: zoom-call
description: Talk with the user by voice on Zoom. Use when the user asks you to call them, to join their Zoom meeting or personal meeting ("join my Zoom", "hop on a call"), or when something is urgent or easier to discuss by voice and the user has said calls are OK.
---

# Zoom call

A voice bot (the user's self-hosted openclaw-zoom-bot) joins a Zoom meeting and talks with the
user. It thinks with this same OpenClaw agent, so on the call it can do anything you can.
You get the transcript when the call ends.

## Setup (once)

Put the server's `API_TOKEN` in a private file the script reads:

```bash
mkdir -p ~/.config/openclaw-zoom-bot
printf 'ZOOM_BOT_TOKEN=%s\n' 'PASTE_API_TOKEN' > ~/.config/openclaw-zoom-bot/skill.env
chmod 600 ~/.config/openclaw-zoom-bot/skill.env
```

Add `ZOOM_BOT_URL=http://...` to the same file if the server isn't at `http://localhost:8090`.

## Call the user

```bash
{baseDir}/scripts/zoom.sh call "Short reason for the call, written as context for the voice bot"
```

This returns right away with JSON including `id` and `join_url`. The bot is now waiting in
the meeting. **Send the user the `join_url` in this chat** so they can tap it and join
(they may also get a push notification, if the server has one set up). Then wait:

```bash
{baseDir}/scripts/zoom.sh wait ID
```

## Always-on link

If the server has an always-on meeting, the bot is always waiting there and the user can just
join it. `zoom.sh status` shows it as `always_on.join_url`; give the user that link if they ask
for it. `zoom.sh call` sends that same link. Each visit to the always-on meeting is its own call.

## Join the user's meeting

```bash
{baseDir}/scripts/zoom.sh join "https://zoom.us/j/...?...pwd=..."   # a link they gave you
{baseDir}/scripts/zoom.sh join                                    # their personal meeting
```

Then `zoom.sh wait ID` as above.

## Results

`wait` prints JSON when the call ends:

- `status`: `completed`, `missed` (nobody joined in time), or `failed` (see `error`)
- `transcript`: list of `{role, text}` turns (`assistant` = the voice bot, `user` = the person)

`zoom.sh hangup ID` makes the bot leave; `zoom.sh status` shows whether the Zoom bridge is
connected and whether a call is already going (only one at a time).

## Rules

- Only call when the user asked to be called, or has said it's OK to call for this kind of thing.
- Put everything the voice bot needs in the reason: what happened, what to ask, what decision
  is needed. It doesn't see this chat.
- If the status is `missed`, don't call again right away. Send a message instead.
- If it fails with "the Zoom bridge isn't connected", tell the user the openclaw-zoom-bot
  containers aren't running.
- After a completed call, act on what the user said in the transcript and confirm in chat
  what you did.
