import os
from pathlib import Path

import numpy as np
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def es_audio() -> np.ndarray:
    from zoomlt.audio.capture import load_audio_16k

    return load_audio_16k(str(FIXTURES / "es_short.wav"))


@pytest.fixture(scope="session")
def hi_audio() -> np.ndarray:
    from zoomlt.audio.capture import load_audio_16k

    return load_audio_16k(str(FIXTURES / "hi_short.wav"))


@pytest.fixture
def integration() -> bool:
    if os.environ.get("ZOOMLT_INTEGRATION") != "1":
        pytest.skip("set ZOOMLT_INTEGRATION=1 to run the real Whisper model")
    return True
