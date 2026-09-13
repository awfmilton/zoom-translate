"""FasterWhisperEngine decision logic with a stub model (no download)."""

import collections
from types import SimpleNamespace

import numpy as np
import pytest

from zoomlt.asr import FasterWhisperEngine
from zoomlt.config import AsrConfig


class StubModel:
    def __init__(self, detect=("ta", 0.6, [("ta", 0.6), ("hi", 0.3), ("es", 0.1)]), segments=()):
        self.detect = detect
        self.segments = list(segments)
        self.detect_calls = 0
        self.transcribe_calls = []

    def detect_language(self, audio=None, **kw):
        self.detect_calls += 1
        return self.detect

    def transcribe(self, audio, **kw):
        self.transcribe_calls.append(kw)
        info = SimpleNamespace(language=kw.get("language") or "en", language_probability=0.5)
        return iter(self.segments), info


def make_engine(model, **cfg_kw):
    cfg = AsrConfig(**cfg_kw)
    eng = FasterWhisperEngine.__new__(FasterWhisperEngine)
    eng.cfg = cfg
    eng.model = model
    import threading

    eng._lock = threading.Lock()
    eng.allowed = [l.lower() for l in cfg.languages]
    eng.fixed_language = eng.allowed[0] if len(eng.allowed) == 1 else None
    eng._lang_cache = collections.OrderedDict()
    eng._last_language = None
    return eng


def seg(text, no_speech=0.0, logprob=-0.3, ratio=1.2):
    return SimpleNamespace(text=text, no_speech_prob=no_speech, avg_logprob=logprob, compression_ratio=ratio)


AUDIO = np.zeros(16000 * 3, dtype=np.float32)


def test_restricted_detection_picks_best_allowed_language():
    model = StubModel()
    eng = make_engine(model, languages=["es", "hi"])
    assert eng._choose_language(AUDIO) == ("hi", 0.3)          # raw argmax was Tamil
    eng2 = make_engine(StubModel(detect=("ja", 0.9, [("ja", 0.9)])), languages=["es", "hi"])
    assert eng2._choose_language(AUDIO) == ("es", 0.0)         # nothing allowed in the list: first choice
    assert make_engine(StubModel(), languages=["hi"])._choose_language(AUDIO) == ("hi", 1.0)
    assert make_engine(StubModel(), languages=[])._choose_language(AUDIO) == (None, 0.0)


def test_detection_runs_once_per_utterance_and_not_on_short_partials():
    model = StubModel()
    eng = make_engine(model, languages=["es", "hi"])
    eng.transcribe(AUDIO, task="translate", is_partial=True, utterance_id=1)
    eng.transcribe(AUDIO, task="translate", is_partial=True, utterance_id=1)
    eng.transcribe(AUDIO, task="translate", is_partial=False, utterance_id=1)
    assert model.detect_calls == 1
    short = np.zeros(16000, dtype=np.float32)
    eng.transcribe(short, task="translate", is_partial=True, utterance_id=2)   # < 2 s: reuse previous language
    assert model.detect_calls == 1
    eng.transcribe(AUDIO, task="translate", is_partial=False, utterance_id=2)
    assert model.detect_calls == 2
    assert model.transcribe_calls[-1]["language"] == "hi"


def test_hallucination_filters_drop_bad_segments():
    segments = [
        seg("Real words here."),
        seg("Subtitles by the Amara.org community"),
        seg("ghost", no_speech=0.9, logprob=-1.5),
        seg("the the the the the the", ratio=3.0),
        seg("   "),
    ]
    eng = make_engine(StubModel(segments=segments), languages=["es"])
    res = eng.transcribe(AUDIO, task="translate")
    assert res.text == "Real words here."
    assert len(res.dropped) == 3
    assert res.no_speech_prob == 0.9


def test_live_decode_parameters():
    model = StubModel(segments=[seg("hola")])
    eng = make_engine(model, languages=["es"], beam_size=1, hotwords="Acme")
    eng.transcribe(AUDIO, task="transcribe")
    kw = model.transcribe_calls[-1]
    assert kw["temperature"] == 0.0 and kw["beam_size"] == 1 and kw["best_of"] == 1
    assert kw["condition_on_previous_text"] is False and kw["vad_filter"] is False
    assert kw["without_timestamps"] is True and kw["hotwords"] == "Acme" and kw["task"] == "transcribe"
