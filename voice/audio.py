"""Audio plumbing: resampling and a real-time paced speaker."""

import asyncio
import logging
import time

import av
import numpy as np

log = logging.getLogger(__name__)

ZOOM_RATE = 32000      # the bridge sends and receives 32 kHz 16-bit mono
FRAME_MS = 20


def resample_pcm(pcm, src_rate, dst_rate):
    """Resample 16-bit mono PCM using libav (via PyAV)."""
    if src_rate == dst_rate or not pcm:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16).reshape(1, -1)
    frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
    frame.sample_rate = src_rate
    resampler = av.AudioResampler(format="s16", layout="mono", rate=dst_rate)
    out = b"".join(f.to_ndarray().tobytes() for f in resampler.resample(frame))
    out += b"".join(f.to_ndarray().tobytes() for f in resampler.resample(None))
    return out


class InputResampler:
    """Turns a stream of 32 kHz chunks (any size) into 20 ms frames of 16 kHz PCM."""

    def __init__(self, src_rate=ZOOM_RATE, rate=16000):
        self.src_rate, self.rate = src_rate, rate
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
        self._pending = bytearray()
        self._frame_bytes = rate * FRAME_MS // 1000 * 2

    def __call__(self, pcm):
        """Returns a list of 640-byte frames (possibly empty)."""
        pcm = pcm[: len(pcm) // 2 * 2]
        if not pcm:
            return []
        frame = av.AudioFrame.from_ndarray(np.frombuffer(pcm, dtype=np.int16).reshape(1, -1),
                                           format="s16", layout="mono")
        frame.sample_rate = self.src_rate
        self._pending += b"".join(f.to_ndarray().tobytes() for f in self._resampler.resample(frame))
        out = []
        while len(self._pending) >= self._frame_bytes:
            out.append(bytes(self._pending[: self._frame_bytes]))
            del self._pending[: self._frame_bytes]
        return out


class Speaker:
    """Plays queued PCM by sending 20 ms frames to `sink` in real time.

    Pacing on this side (instead of handing Zoom the whole reply at once) means a
    barge-in stops the voice right away: clear() drops whatever hasn't been sent yet.
    """

    def __init__(self, sink, rate=ZOOM_RATE):
        self.sink = sink                 # async callable(bytes)
        self.rate = rate
        self._frame_bytes = rate * FRAME_MS // 1000 * 2
        self._buf = bytearray()
        self._idle = asyncio.Event()
        self._idle.set()
        self._wake = asyncio.Event()
        self._task = None

    def start(self):
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self):
        self.clear()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def play(self, pcm, rate):
        """Queue 16-bit mono PCM at any sample rate."""
        self._buf += resample_pcm(pcm, rate, self.rate)
        if self._buf:
            self._idle.clear()
            self._wake.set()

    def clear(self):
        """Stop playback immediately (used for barge-in)."""
        self._buf.clear()
        self._idle.set()

    @property
    def playing(self):
        return bool(self._buf)

    async def wait_idle(self):
        await self._idle.wait()

    async def _run(self):
        frame_s = FRAME_MS / 1000
        while True:
            await self._wake.wait()
            self._wake.clear()
            start, sent = time.monotonic(), 0
            while self._buf:
                chunk = bytes(self._buf[: self._frame_bytes])
                del self._buf[: self._frame_bytes]
                if not self._buf:
                    self._idle.set()
                try:
                    await self.sink(chunk.ljust(self._frame_bytes, b"\x00"))
                except Exception as e:  # the bridge went away; the call ends elsewhere
                    log.debug("speaker sink failed: %r", e)
                sent += 1
                wait = start + sent * frame_s - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
