from pathlib import Path

import pytest

from zoomlt.config import Config

EXAMPLE = Path(__file__).parent.parent / "config.example.toml"


def test_defaults_are_live_friendly():
    cfg = Config()
    assert cfg.asr.mode == "translate"
    assert cfg.asr.model == "small"
    assert cfg.asr.beam_size == 1
    assert cfg.vad.end_silence_ms == 600
    assert cfg.server.host == "127.0.0.1"


def test_example_toml_loads_and_matches_defaults():
    cfg = Config.from_toml(EXAMPLE)
    assert cfg.to_dict() == Config().to_dict()


def test_unknown_key_rejected():
    with pytest.raises(ValueError, match="unknown config key asr.modle"):
        Config.from_dict({"asr": {"modle": "small"}})
    with pytest.raises(ValueError, match="unknown config section"):
        Config.from_dict({"asrr": {}})


def test_validation():
    with pytest.raises(ValueError, match="asr.mode"):
        Config.from_dict({"asr": {"mode": "speak"}})
    with pytest.raises(ValueError, match="audio.file is required"):
        Config.from_dict({"audio": {"backend": "file"}})
    with pytest.raises(ValueError, match="neg_threshold"):
        Config.from_dict({"vad": {"threshold": 0.3, "neg_threshold": 0.5}})
    with pytest.raises(ValueError, match="needs an MT engine"):
        Config.from_dict({"asr": {"mode": "dual"}, "mt": {"engine": "none"}})


def test_apply_overrides_skips_none_and_validates():
    cfg = Config()
    cfg.apply_overrides({"asr.model": "base", "asr.languages": ["es", "hi"], "server.port": None})
    assert cfg.asr.model == "base"
    assert cfg.asr.languages == ["es", "hi"]
    assert cfg.server.port == 8765
    with pytest.raises(ValueError):
        cfg.apply_overrides({"asr.mode": "nope"})


def test_language_codes_and_max_utterance_are_validated():
    with pytest.raises(ValueError, match="ISO 639-1"):
        Config.from_dict({"asr": {"languages": ["spanish"]}})
    with pytest.raises(ValueError, match="max_utterance_s"):
        Config.from_dict({"vad": {"max_utterance_s": 2.0}})


def test_zoom_token_url_is_validated_without_echo():
    with pytest.raises(ValueError) as exc:
        Config.from_dict({"zoom_cc": {"api_token_url": "http://not-https/closedcaption?id=1"}})
    assert "not shown" in str(exc.value) and "not-https" not in str(exc.value)
    Config.from_dict({"zoom_cc": {"api_token_url": "https://wmcc.zoom.us/closedcaption?id=1&signature=x"}})
