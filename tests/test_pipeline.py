import json
import threading
import time

import numpy as np

from zoomlt.asr import AsrResult, FakeEngine
from zoomlt.audio.capture import FileSource
from zoomlt.config import Config
from zoomlt.mt import FakeTranslator
from zoomlt.pipeline import Pipeline


def make_source(es_audio, hi_audio):
    gap = np.zeros(16000, dtype=np.float32)   # 1 s silence between the two clips
    audio = np.concatenate([gap, es_audio, gap, hi_audio, gap])
    return FileSource("synthetic", realtime=False, audio=audio)


def run_pipeline(cfg, engine, translator, source):
    captions, statuses = [], []
    p = Pipeline(cfg, engine, translator, captions.append, statuses.append, status_interval_s=0.01)
    p.run(source)
    return captions, statuses, p


def test_translate_mode_end_to_end(es_audio, hi_audio):
    cfg = Config()
    cfg.asr.model = "fake"
    cfg.asr.partial_interval_ms = 400
    engine = FakeEngine(texts=["The rest of the teams stay at their base.", "Women cannot be stopped."], language="es")
    captions, statuses, p = run_pipeline(cfg, engine, FakeTranslator(), make_source(es_audio, hi_audio))

    finals = [c for c in captions if c.kind == "final"]
    partials = [c for c in captions if c.kind == "partial"]
    assert len(finals) == 2, [c.to_dict() for c in captions]
    assert finals[0].english.startswith("The rest") and finals[1].english.startswith("Women")
    assert all(c.source_text is None for c in finals)          # translate mode: English only
    assert partials, "partials should be emitted during a 4-7 s sentence"
    # every partial belongs to the utterance whose final follows it
    ids = [c.id for c in captions]
    assert ids == sorted(ids)
    assert finals[0].id == 0 and finals[1].id == 1
    # utterance boundaries land near the real clips (1 s gap, 4.7 s clip, 1 s gap, 7.2 s clip)
    assert 0.5 < finals[0].start_s < 2.0 and 4.0 < finals[0].end_s < 6.3
    assert 6.3 < finals[1].start_s < 8.5
    assert engine.calls[-1]["task"] == "translate"
    assert p.stats.finals == 2 and p.stats.utterances == 2
    assert statuses and statuses[-1].utterances == 2


def test_dual_mode_translates_finals_only(es_audio, hi_audio):
    cfg = Config()
    cfg.asr.model = "fake"
    cfg.asr.mode = "dual"
    cfg.mt.engine = "fake"
    engine = FakeEngine(texts=["hola equipo", "namaste"], language="es")
    tr = FakeTranslator(langs={"es"})
    captions, _, _ = run_pipeline(cfg, engine, tr, make_source(es_audio, hi_audio))
    finals = [c for c in captions if c.kind == "final"]
    assert len(finals) == 2
    assert finals[0].source_text == "hola equipo" and finals[0].english == "[es->en] hola equipo"
    assert engine.calls[-1]["task"] == "transcribe"
    assert len(tr.calls) == 2                       # one translation per final, none for partials
    assert sum(1 for c in engine.calls if c["partial"]) >= 1


def test_transcript_file_and_retract(tmp_path, es_audio):
    cfg = Config()
    cfg.asr.model = "fake"
    cfg.server.transcript_path = str(tmp_path / "t.jsonl")

    class EmptyFinalEngine(FakeEngine):
        def transcribe(self, audio, task, is_partial=False, utterance_id=None):
            res = super().transcribe(audio, task, is_partial, utterance_id)
            if not is_partial:
                res = AsrResult(text="", language="es", language_probability=0.5, task=task,
                                duration_s=res.duration_s, decode_s=0.0)
            return res

    source = FileSource("synthetic", realtime=False, audio=np.concatenate([np.zeros(8000, np.float32), es_audio]))
    captions, _, _ = run_pipeline(cfg, EmptyFinalEngine(texts=["partial words"]), FakeTranslator(), source)
    kinds = [c.kind for c in captions]
    assert "partial" in kinds and kinds[-1] == "retract"
    assert (tmp_path / "t.jsonl").read_text() == ""   # nothing final was written


def test_partial_backlog_is_coalesced(es_audio):
    """With the worker blocked, a whole clip's worth of partial ticks collapses to one queued job."""
    cfg = Config()
    cfg.asr.model = "fake"
    cfg.asr.partial_interval_ms = 100
    p = Pipeline(cfg, FakeEngine(), FakeTranslator(), lambda c: None)
    # feed the producer side only (no worker thread): ~45 partial ticks for a 4.7 s clip
    audio = np.concatenate([np.zeros(8000, np.float32), es_audio])
    n_partial_events, max_pending = 0, 0
    for i in range(0, audio.shape[0], 512):
        for ev in p.segmenter.feed(audio[i:i + 512]):
            n_partial_events += ev.kind == "partial"
            p._enqueue(ev)
            max_pending = max(max_pending, sum(1 for j in p._jobs if j.event.kind == "partial"))
    assert n_partial_events >= 10
    assert max_pending == 1                                  # never more than one pending partial
    for ev in p.segmenter.flush():
        p._enqueue(ev)
    assert [j.event.kind for j in p._jobs] == ["final"]     # the final replaced its partial


def test_stop_interrupts_a_running_pipeline(es_audio):
    cfg = Config()
    cfg.asr.model = "fake"
    audio = np.tile(es_audio, 20)  # ~90 s of speech at real-time pace
    source = FileSource("synthetic", realtime=True, audio=audio)
    p = Pipeline(cfg, FakeEngine(), FakeTranslator(), lambda c: None)
    t = threading.Thread(target=p.run, args=(source,), daemon=True)
    t.start()
    time.sleep(0.5)
    p.stop()
    source.stop()
    t.join(timeout=5)
    assert not t.is_alive()


def test_fast_file_playback_keeps_every_sample(es_audio):
    """--fast must not drop audio: 60 s through a FileSource that is not real-time paced."""
    audio = np.tile(es_audio, 13)          # ~61 s, well over the 20 s live drop window
    source = FileSource("synthetic", realtime=False, audio=audio)
    total = 0
    with source:
        for block in source.blocks():
            total += block.shape[0]
    assert total >= audio.shape[0]
    assert source.dropped_seconds() == 0.0


def test_silent_source_warns_but_does_not_hang(caplog):
    from zoomlt.audio.capture import AudioSource

    class Dead(AudioSource):
        def start(self):
            pass

    src = Dead()
    src.silence_warn_s = 0.05
    got = []
    import threading

    def consume():
        with src:
            for b in src.blocks():
                got.append(b)

    t = threading.Thread(target=consume, daemon=True)
    with caplog.at_level("WARNING"):
        t.start()
        time.sleep(0.3)
        src.stop()
        t.join(timeout=2)
    assert not t.is_alive() and got == []
    assert any("no audio from" in r.message for r in caplog.records)


def test_backlog_is_bounded_when_engine_is_slower_than_realtime(es_audio):
    """A recogniser at RTF >> 1 must shed old finals instead of queueing forever."""
    cfg = Config()
    cfg.asr.model = "fake"
    cfg.asr.partial_interval_ms = 0
    gap = np.zeros(12000, np.float32)
    audio = np.concatenate([np.concatenate([es_audio, gap]) for _ in range(8)])   # 8 utterances, ~44 s
    source = FileSource("synthetic", realtime=False, audio=audio)
    engine = FakeEngine(delay_s=0.4)
    captions, _, p = run_pipeline(cfg, engine, FakeTranslator(), source)
    from zoomlt.pipeline import MAX_QUEUED_FINALS

    assert p.stats.utterances == 8
    assert p.stats.shed >= 1
    assert p.stats.finals + p.stats.shed == 8
    assert p.stats.backlog <= MAX_QUEUED_FINALS + 1


def test_partial_window_caps_non_streaming_decodes(es_audio):
    cfg = Config()
    cfg.asr.model = "fake"
    cfg.asr.partial_interval_ms = 300
    cfg.asr.partial_window_s = 1.0
    engine = FakeEngine()
    source = FileSource("synthetic", realtime=False, audio=np.concatenate([np.zeros(8000, np.float32), es_audio, np.zeros(16000, np.float32)]))
    run_pipeline(cfg, engine, FakeTranslator(), source)
    partial_durations = [c["duration"] for c in engine.calls if c["partial"]]
    final_durations = [c["duration"] for c in engine.calls if not c["partial"]]
    assert partial_durations and max(partial_durations) <= 1.01
    assert final_durations and max(final_durations) > 3.0        # the final still sees everything


def test_stop_drops_pending_partials_and_keeps_newest_final(es_audio):
    cfg = Config()
    cfg.asr.model = "fake"
    p = Pipeline(cfg, FakeEngine(delay_s=0.5), FakeTranslator(), lambda c: None)
    from zoomlt.segmenter import SegmentEvent

    for i in range(3):
        p._enqueue(SegmentEvent("partial", es_audio, 0.0, 1.0))
        p._enqueue(SegmentEvent("final", es_audio, 0.0, 1.0, reason="silence"))
    p.stop()
    assert p.stats.backlog <= 1 and all(j.event.kind == "final" for j in p._jobs)


def test_live_source_drops_oldest_and_resamples():
    """A live source past the queue cap drops the oldest blocks, and blocks() resamples to 16 kHz."""
    from zoomlt.audio.capture import AudioSource, _MAX_QUEUED_SECONDS, _match_device

    class Live(AudioSource):
        native_rate = 48000
        channels = 2

        def start(self):
            pass

    src = Live(block_ms=100)
    block = np.zeros((4800, 2), dtype=np.float32)
    n = int(_MAX_QUEUED_SECONDS * 10) + 30
    for _ in range(n):
        src._push(block)
    assert src._dropped >= 25 and abs(src.dropped_seconds() - src._dropped * 0.1) < 1e-6
    src.stop()
    total = 0
    with src:
        for out in src.blocks():
            assert out.dtype == np.float32 and out.ndim == 1
            total += out.shape[0]
    kept = n - src._dropped
    assert abs(total - kept * 1600) < 1600 * 2

    devices = [{"name": "Microphone (Realtek)", "max_input_channels": 2},
               {"name": "Speakers", "max_input_channels": 0},
               {"name": "BlackHole 2ch", "max_input_channels": 2}]
    assert _match_device(devices, None) is None
    assert _match_device(devices, "1") == 1
    assert _match_device(devices, "blackhole") == 2
    import pytest

    with pytest.raises(ValueError, match="no input device"):
        _match_device(devices, "speakers")       # an output-only device never matches by name
    with pytest.raises(ValueError, match="no input device"):
        _match_device(devices, "nonexistent")
