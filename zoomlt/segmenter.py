"""Turn a continuous 16 kHz stream into utterances using the streaming VAD.

State machine per 32 ms frame:

    silence ──speech prob > threshold──▶ speech
    speech  ──prob < neg_threshold for end_silence_ms──▶ silence  (emit FINAL)
    speech  ──every partial_interval_ms──▶ emit PARTIAL (same utterance, growing)
    speech  ──longer than max_utterance_s──▶ cut at the quietest recent frame (emit FINAL, keep tail)

Only audio the VAD called speech (plus a short pre-roll) ever reaches Whisper, which is the single
most effective defence against hallucinated captions on silence.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, List, Optional

import numpy as np

from zoomlt.config import VadConfig
from zoomlt.vad import FRAME_MS, FRAME_SAMPLES, SAMPLE_RATE


@dataclass
class SegmentEvent:
    kind: str            # "partial" | "final"
    audio: np.ndarray    # 16 kHz mono float32
    start_s: float       # stream time of the first sample
    end_s: float         # stream time just after the last sample
    reason: str = ""     # "silence" | "max-length" | "flush" | "" (partial)

    @property
    def duration_s(self) -> float:
        return self.audio.shape[0] / SAMPLE_RATE


class Segmenter:
    def __init__(self, vad: Callable[[np.ndarray], float], cfg: VadConfig, partial_interval_ms: int = 1200):
        self.vad = vad
        self.cfg = cfg
        self.partial_interval_frames = int(partial_interval_ms // FRAME_MS) if partial_interval_ms > 0 else 0
        self.end_silence_frames = max(1, int(cfg.end_silence_ms // FRAME_MS))
        self.min_speech_frames = max(1, int(cfg.min_speech_ms // FRAME_MS))
        self.max_frames = max(self.min_speech_frames + 1, int(cfg.max_utterance_s * 1000 // FRAME_MS))
        self.cut_search_frames = max(1, int(cfg.cut_search_ms // FRAME_MS))
        pre_roll_frames = max(0, int(cfg.pre_roll_ms // FRAME_MS))
        self._pre_roll: Deque[np.ndarray] = deque(maxlen=pre_roll_frames or 1)
        self._pre_roll_enabled = pre_roll_frames > 0
        self._pending = np.zeros(0, dtype=np.float32)
        self._frame_no = 0               # frames consumed since start (stream clock)
        self.reset_utterance()
        self.in_speech = False
        self.last_prob = 0.0

    # -- helpers ------------------------------------------------------------------------------

    def reset_utterance(self) -> None:
        self._frames: List[np.ndarray] = []
        self._probs: List[float] = []
        self._start_frame = 0
        self._speech_frames = 0
        self._silence_run = 0
        self._since_partial = 0

    @property
    def stream_time_s(self) -> float:
        return self._frame_no * FRAME_MS / 1000.0

    def _event(self, kind: str, frames: List[np.ndarray], start_frame: int, reason: str = "") -> SegmentEvent:
        audio = np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)
        return SegmentEvent(
            kind=kind,
            audio=audio,
            start_s=start_frame * FRAME_MS / 1000.0,
            end_s=(start_frame + len(frames)) * FRAME_MS / 1000.0,
            reason=reason,
        )

    # -- public ---------------------------------------------------------------------------------

    def feed(self, block: np.ndarray) -> List[SegmentEvent]:
        """Consume an arbitrary-length 16 kHz block, return zero or more events."""
        events: List[SegmentEvent] = []
        data = np.concatenate([self._pending, np.ascontiguousarray(block, dtype=np.float32)])
        n_full = data.shape[0] // FRAME_SAMPLES
        for i in range(n_full):
            frame = data[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES]
            events.extend(self._step(frame))
        self._pending = data[n_full * FRAME_SAMPLES :]
        return events

    def flush(self) -> List[SegmentEvent]:
        """End of stream: emit whatever utterance is open."""
        events: List[SegmentEvent] = []
        if self.in_speech and self._speech_frames >= self.min_speech_frames:
            events.append(self._event("final", self._frames, self._start_frame, reason="flush"))
        self.in_speech = False
        self.reset_utterance()
        return events

    # -- core -----------------------------------------------------------------------------------

    def _step(self, frame: np.ndarray) -> List[SegmentEvent]:
        events: List[SegmentEvent] = []
        prob = self.vad(frame)
        self.last_prob = prob
        frame_no = self._frame_no
        self._frame_no += 1

        if not self.in_speech:
            if prob >= self.cfg.threshold:
                self.in_speech = True
                self.reset_utterance()
                pre = list(self._pre_roll) if self._pre_roll_enabled else []
                self._frames = pre + [frame]
                self._probs = [0.0] * len(pre) + [prob]
                self._start_frame = frame_no - len(pre)
                self._speech_frames = 1
            else:
                if self._pre_roll_enabled:
                    self._pre_roll.append(frame)
            return events

        # in speech
        self._frames.append(frame)
        self._probs.append(prob)
        self._since_partial += 1
        if prob >= self.cfg.threshold:
            self._speech_frames += 1
        # Silence is counted in CONSECUTIVE frames below neg_threshold; anything above it (soft
        # speech between the two thresholds included) restarts the count, so the trailing trim
        # below only ever removes a contiguous run of silence.
        if prob < self.cfg.neg_threshold:
            self._silence_run += 1
        else:
            self._silence_run = 0

        if self._silence_run >= self.end_silence_frames:
            # Utterance over. Trim most of the trailing silence; keep ~1 frame of padding.
            keep = len(self._frames) - self._silence_run + 1
            frames = self._frames[:keep]
            if self._speech_frames >= self.min_speech_frames:
                events.append(self._event("final", frames, self._start_frame, reason="silence"))
            self.in_speech = False
            if self._pre_roll_enabled:
                self._pre_roll.clear()
                for f in self._frames[keep:]:
                    self._pre_roll.append(f)
            self.reset_utterance()
            return events

        if len(self._frames) >= self.max_frames:
            cut = self._quietest_cut()
            head, tail = self._frames[:cut], self._frames[cut:]
            head_probs, tail_probs = self._probs[:cut], self._probs[cut:]
            events.append(self._event("final", head, self._start_frame, reason="max-length"))
            self._start_frame += cut
            self._frames, self._probs = tail, tail_probs
            self._speech_frames = sum(1 for p in tail_probs if p >= self.cfg.threshold)
            self._since_partial = 0
            return events

        if self.partial_interval_frames and self._since_partial >= self.partial_interval_frames:
            if self._speech_frames >= self.min_speech_frames:
                events.append(self._event("partial", self._frames, self._start_frame))
            self._since_partial = 0
        return events

    def _quietest_cut(self) -> int:
        """Index to split the utterance: the lowest-probability frame in the search window.

        The head keeps at least min_speech_frames so a cut can never produce a sliver caption.
        """
        n = len(self._probs)
        lo = max(self.min_speech_frames, n - self.cut_search_frames)
        if lo >= n - 1:
            return max(1, n // 2)
        window = self._probs[lo:]
        return lo + int(np.argmin(window))

    def skip(self, seconds: float) -> List[SegmentEvent]:
        """Audio was lost (capture overrun). Close the open utterance and advance the clock."""
        events = self.flush()
        for ev in events:
            ev.reason = "gap"
        self._frame_no += int(round(seconds * 1000 / FRAME_MS))
        self._pending = np.zeros(0, dtype=np.float32)
        if self._pre_roll_enabled:
            self._pre_roll.clear()
        return events
