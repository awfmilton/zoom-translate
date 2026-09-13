import numpy as np

from zoomlt.audio.resample import Resampler, to_mono


def test_to_mono_averages_channels():
    stereo = np.stack([np.ones(10), -np.ones(10)], axis=1).astype(np.float32)
    mono = to_mono(stereo)
    assert mono.shape == (10,)
    assert np.allclose(mono, 0.0)
    assert to_mono(np.ones((5, 1), dtype=np.float32)).shape == (5,)


def test_passthrough_at_target_rate():
    r = Resampler(16000)
    x = np.random.default_rng(0).standard_normal(1000).astype(np.float32)
    assert np.array_equal(r.process(x), x)


def _sine(rate, seconds, freq=440.0):
    t = np.arange(int(rate * seconds)) / rate
    return np.sin(2 * np.pi * freq * t).astype(np.float32)


def test_numpy_fallback_ratio_and_continuity():
    r = Resampler(48000)
    r._av = None  # force the numpy path
    src = _sine(48000, 1.0)
    out = np.concatenate([r.process(chunk) for chunk in np.array_split(src, 37)])
    assert abs(out.shape[0] - 16000) <= 40
    # a 440 Hz tone stays a 440 Hz tone: check zero-crossing count is about right
    crossings = np.sum(np.diff(np.signbit(out)) != 0)
    assert 860 <= crossings <= 900


def test_pyav_path_if_available():
    r = Resampler(44100)
    if r._av is None:
        return  # PyAV missing; covered by the numpy test
    src = _sine(44100, 0.5)
    out = np.concatenate([r.process(chunk) for chunk in np.array_split(src, 11)])
    assert abs(out.shape[0] - 8000) <= 200
    assert out.dtype == np.float32
    assert np.abs(out).max() <= 1.05
