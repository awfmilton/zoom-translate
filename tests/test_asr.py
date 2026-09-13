import numpy as np
import pytest

from zoomlt.asr import FakeEngine, FasterWhisperEngine, _looks_hallucinated, is_transcribe_only
from zoomlt.config import AsrConfig


def test_transcribe_only_models_detected():
    assert is_transcribe_only("large-v3-turbo")
    assert is_transcribe_only("deepdml/faster-whisper-large-v3-turbo-ct2")
    assert is_transcribe_only("distil-large-v3")
    assert not is_transcribe_only("small")
    assert not is_transcribe_only("large-v3")


def test_translate_mode_refuses_turbo_before_loading_anything():
    with pytest.raises(ValueError, match="transcription-only"):
        FasterWhisperEngine(AsrConfig(model="large-v3-turbo", mode="translate"))


def test_hallucination_heuristics():
    drop = AsrConfig().drop_phrases
    assert _looks_hallucinated("Subtitles by the Amara.org community", drop)
    assert _looks_hallucinated("Thank you for watching!", drop)
    assert _looks_hallucinated("the the the the the the the", drop)
    assert not _looks_hallucinated("We will discuss the budget tomorrow.", drop)
    assert not _looks_hallucinated("Thank you.", drop)   # short genuine thanks survive


def test_fake_engine_sequence_and_partials():
    eng = FakeEngine(texts=["uno", "dos"], language="es")
    audio = np.zeros(16000, dtype=np.float32)
    assert eng.transcribe(audio, "translate", is_partial=True).text == "uno"
    assert eng.transcribe(audio, "translate").text == "uno"
    assert eng.transcribe(audio, "translate").text == "dos"
    assert eng.transcribe(audio, "translate").text == "dos"   # sticks at the last one
    assert eng.calls[0]["partial"] is True and eng.calls[1]["partial"] is False
