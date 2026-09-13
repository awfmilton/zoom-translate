"""Convert whatever the sound card gives us into 16 kHz mono float32.

Zoom plays back at 44.1 or 48 kHz stereo; Whisper and the VAD want 16 kHz mono. PyAV (already
installed as a faster-whisper dependency) wraps libswresample, which is far better than linear
interpolation and costs nothing extra. A numpy fallback keeps tests independent of PyAV.
"""

from __future__ import annotations

import numpy as np

TARGET_RATE = 16000


def to_mono(block: np.ndarray) -> np.ndarray:
    """(frames, channels) or (frames,) -> (frames,) float32 by averaging channels."""
    if block.ndim == 2:
        if block.shape[1] == 1:
            block = block[:, 0]
        else:
            block = block.mean(axis=1)
    return np.ascontiguousarray(block, dtype=np.float32)


class Resampler:
    """Stateful streaming resampler: feed consecutive mono blocks, get 16 kHz mono blocks."""

    def __init__(self, in_rate: int, out_rate: int = TARGET_RATE):
        self.in_rate = int(in_rate)
        self.out_rate = int(out_rate)
        self._av = None
        self._pts = 0
        if self.in_rate != self.out_rate:
            try:
                import av  # PyAV

                self._av = av
                self._resampler = av.AudioResampler(format="flt", layout="mono", rate=self.out_rate)
            except Exception:  # pragma: no cover - PyAV missing; numpy fallback below
                self._av = None
        self._carry = np.zeros(0, dtype=np.float32)

    def process(self, mono: np.ndarray) -> np.ndarray:
        mono = np.ascontiguousarray(mono, dtype=np.float32)
        if self.in_rate == self.out_rate or mono.size == 0:
            return mono
        if self._av is not None:
            return self._process_av(mono)
        return self._process_numpy(mono)

    # -- PyAV / libswresample -----------------------------------------------------------------

    def _process_av(self, mono: np.ndarray) -> np.ndarray:
        av = self._av
        frame = av.AudioFrame.from_ndarray(mono.reshape(1, -1), format="flt", layout="mono")
        frame.sample_rate = self.in_rate
        frame.pts = self._pts
        self._pts += mono.shape[0]
        out = [f.to_ndarray().reshape(-1) for f in self._resampler.resample(frame)]
        if not out:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(out).astype(np.float32, copy=False)

    # -- numpy fallback (linear interpolation, good enough for speech) ------------------------

    def _process_numpy(self, mono: np.ndarray) -> np.ndarray:
        data = np.concatenate([self._carry, mono])
        ratio = self.in_rate / self.out_rate
        n_out = int(np.floor((data.shape[0] - 1) / ratio))
        if n_out <= 0:
            self._carry = data
            return np.zeros(0, dtype=np.float32)
        positions = np.arange(n_out, dtype=np.float64) * ratio
        idx = positions.astype(np.int64)
        frac = (positions - idx).astype(np.float32)
        out = data[idx] * (1.0 - frac) + data[idx + 1] * frac
        consumed = int(np.ceil(n_out * ratio))
        self._carry = data[consumed:]
        return out.astype(np.float32, copy=False)
