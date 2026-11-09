"""Streaming client for any OpenAI-compatible chat completions endpoint."""

import json
import re

import aiohttp

# Split after sentence-ending punctuation followed by whitespace.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


class LLMError(RuntimeError):
    """The LLM endpoint refused or failed. `status` is the HTTP status, if any."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _error_message(data):
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)
    return str(err) if err else None


async def stream_chat(cfg, messages, user=None):
    """Yield text deltas from the model."""
    body = {"model": cfg.llm_model, "messages": messages, "stream": True}
    if user:
        body["user"] = user
    headers = {"Authorization": f"Bearer {cfg.llm_api_key}"} if cfg.llm_api_key else {}
    timeout = aiohttp.ClientTimeout(total=120, sock_read=60)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(cfg.llm_url, json=body, headers=headers) as r:
            if r.status >= 400:
                raise LLMError(f"LLM returned {r.status}: {(await r.text())[:300]}", r.status)
            if "text/event-stream" not in r.headers.get("Content-Type", ""):
                # Server ignored stream=true and sent one JSON response.
                data = await r.json()
                yield data["choices"][0]["message"]["content"] or ""
                return
            async for raw in r.content:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    return
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                # Some servers report failures (like an upstream rate limit) inside the stream.
                if message := _error_message(data):
                    raise LLMError(f"LLM stream error: {message[:300]}")
                try:
                    delta = data["choices"][0].get("delta", {}).get("content")
                except (KeyError, IndexError, TypeError):
                    continue
                if delta:
                    yield delta


async def speakable(deltas):
    """Drop text that shouldn't be read aloud: {JSON}, ```code```, <think>…</think> and markdown symbols.

    Small models sometimes write tool calls or reasoning into their reply instead of using
    the proper channels; this keeps them from being spoken.
    """
    depth, in_code, in_think, ticks, pending = 0, False, False, 0, ""
    async for delta in deltas:
        out = []
        text = pending + delta
        pending = ""
        i = 0
        while i < len(text):
            if in_think:
                end = text.find("</think>", i)
                if end == -1:
                    pending = text[max(i, len(text) - 7):]  # keep a possible partial "</think>"
                    break
                in_think, i = False, end + len("</think>")
                continue
            ch = text[i]
            if ch == "<" and not in_code and depth == 0:
                if text.startswith("<think>", i):
                    in_think, i = True, i + len("<think>")
                    continue
                if "<think>".startswith(text[i:]):  # might be the start of "<think>"
                    pending = text[i:]
                    break
            if ch == "`":
                ticks += 1
                i += 1
                continue
            if ticks:
                if ticks >= 3:
                    in_code = not in_code
                ticks = 0
            if in_code:
                i += 1
                continue
            if ch == "{":
                depth += 1
            elif ch == "}" and depth:
                depth -= 1
            elif depth == 0 and ch not in "*#_":
                out.append(ch)
            i += 1
        if out:
            yield "".join(out)


async def stream_sentences(deltas):
    """Group a stream of text deltas into speakable sentences."""
    buf = ""
    async for delta in deltas:
        buf += delta
        parts = _SENTENCE_END.split(buf)
        for sentence in parts[:-1]:
            if sentence.strip():
                yield sentence.strip()
        buf = parts[-1]
    if buf.strip():
        yield buf.strip()
