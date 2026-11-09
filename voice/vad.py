"""Streaming speech detection with the Silero VAD model that ships inside faster-whisper.

Unlike a loudness threshold, it tells speech apart from background noise, music,
clicks and most echo of the AI's own voice.
"""

import os

import numpy as np

CHUNK = 512     # samples per model step at 16 kHz (32 ms)
CONTEXT = 64    # samples carried over between steps


def _model_path():
    from faster_whisper.utils import get_assets_path

    for name in sorted(os.listdir(get_assets_path()), reverse=True):
        if name.startswith("silero") and name.endswith(".onnx"):
            return os.path.join(get_assets_path(), name)
    raise FileNotFoundError("Silero VAD model not found in faster-whisper's assets")


class SileroStream:
    """Feed 16 kHz 16-bit mono PCM in any chunk size; get speech probabilities out."""

    _session = None  # shared across calls; each stream keeps its own state

    def __init__(self):
        if SileroStream._session is None:
            import onnxruntime

            opts = onnxruntime.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            opts.log_severity_level = 4
            SileroStream._session = onnxruntime.InferenceSession(
                _model_path(), providers=["CPUExecutionProvider"], sess_options=opts)
        self.reset()

    def reset(self):
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)

    def __call__(self, pcm16k):
        """Return one speech probability (0..1) per completed 32 ms step."""
        audio = np.frombuffer(pcm16k, dtype=np.int16).astype(np.float32) / 32768.0
        self._pending = np.concatenate([self._pending, audio])
        probs = []
        while len(self._pending) >= CHUNK:
            chunk, self._pending = self._pending[:CHUNK], self._pending[CHUNK:]
            x = np.concatenate([self._context, chunk])[None, :]
            out, self._h, self._c = self._session.run(None, {"input": x, "h": self._h, "c": self._c})
            self._context = chunk[-CONTEXT:]
            probs.append(float(np.ravel(out)[0]))
        return probs
