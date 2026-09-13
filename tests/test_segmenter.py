import numpy as np

from zoomlt.config import VadConfig
from zoomlt.segmenter import Segmenter
from zoomlt.vad import FRAME_MS, FRAME_SAMPLES


class ScriptedVad:
    """Returns a scripted probability per frame (frames beyond the script are silence)."""

    def __init__(self, probs):
        self.probs = list(probs)
        self.i = 0

    def __call__(self, frame):
        p = self.probs[self.i] if self.i < len(self.probs) else 0.0
        self.i += 1
        return p


def frames(n, value=0.5):
    return np.full(n * FRAME_SAMPLES, value, dtype=np.float32)


def run(seg, n_frames, block_frames=3):
    events = []
    audio = np.arange(n_frames * FRAME_SAMPLES, dtype=np.float32)  # sample index as value: lets us check boundaries
    step = block_frames * FRAME_SAMPLES
    for i in range(0, audio.shape[0], step):
        events.extend(seg.feed(audio[i:i + step]))
    return events


def cfg(**kw):
    base = dict(end_silence_ms=FRAME_MS * 5, pre_roll_ms=FRAME_MS * 3, min_speech_ms=FRAME_MS * 4,
                max_utterance_s=100.0, cut_search_ms=FRAME_MS * 10)
    base.update(kw)
    return VadConfig(**base)


def test_final_on_silence_with_pre_roll():
    probs = [0.0] * 10 + [0.9] * 20 + [0.0] * 10
    seg = Segmenter(ScriptedVad(probs), cfg(), partial_interval_ms=0)
    events = run(seg, 40)
    assert [e.kind for e in events] == ["final"]
    ev = events[0]
    assert ev.reason == "silence"
    # starts 3 pre-roll frames before the first speech frame (frame 10 -> 7)
    assert ev.audio[0] == 7 * FRAME_SAMPLES
    assert abs(ev.start_s - 7 * FRAME_MS / 1000) < 1e-9
    # trailing silence trimmed to about one frame of padding
    assert 20 + 3 <= ev.audio.shape[0] // FRAME_SAMPLES <= 20 + 3 + 2
    assert not seg.in_speech


def test_partials_then_final():
    probs = [0.9] * 30 + [0.0] * 10
    seg = Segmenter(ScriptedVad(probs), cfg(pre_roll_ms=0), partial_interval_ms=FRAME_MS * 10)
    events = run(seg, 40)
    kinds = [e.kind for e in events]
    assert kinds == ["partial", "partial", "final"] or kinds == ["partial", "partial", "partial", "final"]
    # partials grow and all share the utterance start
    assert all(e.start_s == 0.0 for e in events)
    assert events[0].audio.shape[0] < events[-1].audio.shape[0]


def test_too_short_speech_is_ignored():
    probs = [0.9] * 2 + [0.0] * 10
    seg = Segmenter(ScriptedVad(probs), cfg(), partial_interval_ms=0)
    assert run(seg, 20) == []


def test_hysteresis_keeps_utterance_between_thresholds():
    # dips to 0.4 (between neg 0.35 and threshold 0.5) must not end the utterance
    probs = [0.9] * 6 + [0.4] * 8 + [0.9] * 6 + [0.0] * 10
    seg = Segmenter(ScriptedVad(probs), cfg(pre_roll_ms=0), partial_interval_ms=0)
    events = run(seg, 40)
    assert len(events) == 1 and events[0].kind == "final"
    assert events[0].audio.shape[0] // FRAME_SAMPLES >= 20


def test_max_length_cuts_at_quietest_frame():
    # 40 frames of speech with a dip at frame 33; max 36 frames -> cut must land on the dip
    probs = [0.9] * 40
    probs[33] = 0.2
    probs += [0.0] * 10
    seg = Segmenter(ScriptedVad(probs), cfg(pre_roll_ms=0, max_utterance_s=36 * FRAME_MS / 1000), partial_interval_ms=0)
    events = run(seg, 50, block_frames=1)
    assert [e.kind for e in events] == ["final", "final"]
    first, second = events
    assert first.reason == "max-length"
    assert first.audio.shape[0] // FRAME_SAMPLES == 33
    assert second.audio[0] == 33 * FRAME_SAMPLES
    assert abs(second.start_s - 33 * FRAME_MS / 1000) < 1e-9
    assert second.reason == "silence"


def test_flush_emits_open_utterance():
    probs = [0.9] * 10
    seg = Segmenter(ScriptedVad(probs), cfg(pre_roll_ms=0), partial_interval_ms=0)
    assert run(seg, 10) == []
    events = seg.flush()
    assert len(events) == 1 and events[0].reason == "flush"
    assert seg.flush() == []


def test_pending_samples_carry_over():
    seg = Segmenter(ScriptedVad([0.0] * 100), cfg(), partial_interval_ms=0)
    seg.feed(np.zeros(FRAME_SAMPLES + 100, dtype=np.float32))
    assert seg._frame_no == 1 and seg._pending.shape[0] == 100
    seg.feed(np.zeros(FRAME_SAMPLES - 100, dtype=np.float32))
    assert seg._frame_no == 2 and seg._pending.shape[0] == 0


def test_soft_speech_between_thresholds_does_not_accumulate_silence():
    # low frames interleaved with soft speech: 6 low frames in total but never 5 in a row
    probs = [0.9] * 6 + [0.2, 0.45] * 6 + [0.9] * 4 + [0.0] * 10
    seg = Segmenter(ScriptedVad(probs), cfg(pre_roll_ms=0), partial_interval_ms=0)
    events = run(seg, 40, block_frames=1)
    assert len(events) == 1 and events[0].kind == "final"
    # the utterance covers all 22 voiced frames (+1 frame of padding), soft frames included
    assert events[0].audio.shape[0] // FRAME_SAMPLES == 22 + 1


def test_max_length_cut_never_yields_slivers():
    # cut search window as long as the utterance: the head must still keep min_speech frames
    probs = [0.9] * 200
    seg = Segmenter(ScriptedVad(probs), cfg(pre_roll_ms=0, max_utterance_s=12 * FRAME_MS / 1000,
                                            cut_search_ms=FRAME_MS * 40), partial_interval_ms=0)
    events = run(seg, 200, block_frames=1)
    assert events and all(e.audio.shape[0] // FRAME_SAMPLES >= 4 for e in events)


def test_skip_closes_utterance_and_advances_clock():
    seg = Segmenter(ScriptedVad([0.9] * 100), cfg(pre_roll_ms=0), partial_interval_ms=0)
    run(seg, 10)
    events = seg.skip(2.0)
    assert len(events) == 1 and events[0].reason == "gap"
    assert abs(seg.stream_time_s - (10 * FRAME_MS / 1000 + 2.0)) < 0.04
    assert not seg.in_speech


def test_no_partial_during_end_of_utterance_countdown():
    # 12 speech frames then 4 low frames (countdown of 5 not yet reached) then more speech
    probs = [0.9] * 12 + [0.1] * 4 + [0.9] * 10 + [0.0] * 10
    seg = Segmenter(ScriptedVad(probs), cfg(pre_roll_ms=0), partial_interval_ms=FRAME_MS * 13)
    events = run(seg, 40, block_frames=1)
    kinds = [(e.kind, e.audio.shape[0] // FRAME_SAMPLES) for e in events]
    # the partial due at frame 13 (inside the low run) is deferred until speech resumes
    assert kinds[0][0] == "partial" and kinds[0][1] >= 17
    assert kinds[-1][0] == "final"
