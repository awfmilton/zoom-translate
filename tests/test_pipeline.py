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
    assert statuses and statuses[-1].finals >= 0


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
    partial_calls = [c for c in tr.calls if c[0] not in ("hola equipo", "namaste")]
    assert not partial_calls
    assert len(tr.calls) == 2


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
    """A slow engine must not queue up a partial per tick; at most one pending per utterance."""
    cfg = Config()
    cfg.asr.model = "fake"
    cfg.asr.partial_interval_ms = 100
    engine = FakeEngine(texts=["slow"], delay_s=0.15)
    source = FileSource("synthetic", realtime=False, audio=np.concatenate([np.zeros(8000, np.float32), es_audio, np.zeros(16000, np.float32)]))
    captions, _, p = run_pipeline(cfg, engine, FakeTranslator(), source)
    partial_calls = sum(1 for c in engine.calls if c["partial"])
    # ~45 ticks were generated for a 4.7 s clip at 100 ms; only a handful survive coalescing
    assert 1 <= partial_calls <= 6, partial_calls
    assert [c.kind for c in captions][-1] == "final"


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
