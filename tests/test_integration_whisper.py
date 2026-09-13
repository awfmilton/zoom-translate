"""Real model tests. Skipped unless ZOOMLT_INTEGRATION=1 (downloads Whisper 'tiny', ~75 MB)."""

import os

import numpy as np

from zoomlt.asr import FasterWhisperEngine
from zoomlt.config import AsrConfig

MODEL = os.environ.get("ZOOMLT_INTEGRATION_MODEL", "tiny")


def test_whisper_translates_spanish_clip(integration, es_audio):
    eng = FasterWhisperEngine(AsrConfig(model=MODEL, device="cpu", compute_type="int8", languages=["es"]))
    res = eng.transcribe(es_audio, task="translate")
    assert res.language == "es"
    assert res.text, "expected some English text"
    assert res.decode_s > 0
    words = set(res.text.lower().replace(".", "").split())
    assert words & {"rest", "teams", "team", "equipment", "remain", "remains", "stay", "stays", "headquarters", "base"}, res.text


def test_language_restriction_picks_from_allowed_set(integration, hi_audio):
    eng = FasterWhisperEngine(AsrConfig(model=MODEL, device="cpu", compute_type="int8", languages=["es", "hi"]))
    res = eng.transcribe(hi_audio, task="transcribe")
    assert res.language in ("es", "hi")


def test_silence_yields_nothing(integration):
    eng = FasterWhisperEngine(AsrConfig(model=MODEL, device="cpu", compute_type="int8", languages=["es"]))
    res = eng.transcribe(np.zeros(16000 * 2, dtype=np.float32), task="translate")
    assert res.text == "" or res.dropped
