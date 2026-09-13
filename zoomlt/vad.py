"""Streaming Silero VAD.

faster-whisper ships the Silero VAD v6 ONNX model but only exposes a batch API that resets the
recurrent state on every call. Live audio needs the opposite: one 32 ms frame at a time with the
LSTM state carried across calls. This wrapper keeps that state, so it has no dependency beyond
onnxruntime and the model file already installed with faster-whisper.
"""

from __future__ import annotations

import os
from typing import Optional

import numpy as np

SAMPLE_RATE = 16000
FRAME_SAMPLES = 512          # Silero v5/v6 is trained on 512-sample frames at 16 kHz (32 ms)
_CONTEXT_SAMPLES = 64        # the model wants the last 64 samples of the previous frame prepended
FRAME_MS = FRAME_SAMPLES * 1000 // SAMPLE_RATE


def default_model_path() -> str:
    """Locate the Silero ONNX file bundled with faster-whisper."""
    from faster_whisper.utils import get_assets_path  # local import: keeps tests light

    path = os.path.join(get_assets_path(), "silero_vad_v6.onnx")
    if not os.path.exists(path):  # pragma: no cover - only with an unexpected faster-whisper build
        candidates = [f for f in os.listdir(get_assets_path()) if f.startswith("silero_vad") and f.endswith(".onnx")]
        if not candidates:
            raise FileNotFoundError("no silero_vad*.onnx in faster-whisper assets")
        path = os.path.join(get_assets_path(), sorted(candidates)[-1])
    return path


class StreamingVad:
    """Frame-by-frame speech probability with persistent LSTM state.

    >>> vad = StreamingVad()
    >>> prob = vad(frame)   # frame: np.float32[512] in [-1, 1]
    """

    def __init__(self, model_path: Optional[str] = None):
        import onnxruntime  # noqa: WPS433 - optional heavy import kept local

        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.enable_cpu_mem_arena = False
        opts.log_severity_level = 4
        self._session = onnxruntime.InferenceSession(
            model_path or default_model_path(), providers=["CPUExecutionProvider"], sess_options=opts
        )
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, _CONTEXT_SAMPLES), dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        if frame.ndim != 1 or frame.shape[0] != FRAME_SAMPLES:
            raise ValueError(f"VAD frame must be 1-D with {FRAME_SAMPLES} samples, got {frame.shape}")
        if frame.dtype != np.float32:
            frame = frame.astype(np.float32)
        x = np.concatenate([self._context, frame.reshape(1, -1)], axis=1)
        out, self._h, self._c = self._session.run(None, {"input": x, "h": self._h, "c": self._c})
        self._context = frame[-_CONTEXT_SAMPLES:].reshape(1, -1).copy()
        return float(out.reshape(-1)[0])
