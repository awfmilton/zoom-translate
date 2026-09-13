import json
from pathlib import Path

from zoomlt.cli import build_config, main, make_parser

FIX = Path(__file__).parent / "fixtures"


def test_build_config_from_args():
    args = make_parser().parse_args(["run", "--model", "base", "-l", "es", "-l", "hi", "--no-partials",
                                     "--port", "9000", "--zoom-cc-url", "https://x/y?id=1", "--mode", "dual"])
    cfg = build_config(args)
    assert cfg.asr.model == "base" and cfg.asr.languages == ["es", "hi"]
    assert cfg.asr.partial_interval_ms == 0 and cfg.server.port == 9000
    assert cfg.zoom_cc.api_token_url == "https://x/y?id=1" and cfg.asr.mode == "dual"


def test_check_command_prints_effective_config(capsys):
    try:
        import sounddevice  # noqa: F401

        portaudio_ok = True
    except Exception:
        portaudio_ok = False
    rc = main(["check", "--model", "fake", "--no-browser"])
    out = capsys.readouterr().out
    assert rc == (0 if portaudio_ok else 1)
    assert ("ok   sounddevice" if portaudio_ok else "MISSING sounddevice") in out
    assert '"model": "fake"' in out


def test_runtime_errors_are_clean(monkeypatch, capsys):
    import zoomlt.cli as cli

    monkeypatch.setattr(cli, "_run_pipeline", lambda cfg: (_ for _ in ()).throw(RuntimeError("PortAudio library not found")))
    assert main(["run"]) == 2
    assert "PortAudio library not found" in capsys.readouterr().err


def test_check_hides_the_zoom_token(capsys):
    main(["check", "--model", "fake", "--zoom-cc-url", "https://wmcc.zoom.us/closedcaption?id=1&signature=SECRET"])
    out = capsys.readouterr().out
    assert "SECRET" not in out and "<set, hidden>" in out


def test_bad_value_is_a_clean_error(capsys):
    rc = main(["check", "--mode", "translate", "--model", "large-v3-turbo", "--backend", "file"])
    assert rc == 2
    assert "audio.file is required" in capsys.readouterr().err


def test_file_command_end_to_end_with_fake_engine(capsys, tmp_path):
    transcript = tmp_path / "t.jsonl"
    rc = main(["file", str(FIX / "es_short.wav"), "--fast", "--model", "fake", "--no-browser",
               "--port", "0", "--transcript", str(transcript)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Overlay: http://127.0.0.1:" in out
    lines = [json.loads(l) for l in transcript.read_text().splitlines()]
    assert len(lines) >= 1 and lines[0]["kind"] == "final" and lines[0]["english"].startswith("translate")
    assert "captions, real-time factor" in out


def test_parec_backend_requires_the_binary(monkeypatch):
    import shutil

    from zoomlt.audio.capture import ParecSource

    monkeypatch.setattr(shutil, "which", lambda name: None)
    import pytest

    with pytest.raises(RuntimeError, match="parec not found"):
        ParecSource()


def test_env_var_supplies_zoom_token(monkeypatch):
    monkeypatch.setenv("ZOOMLT_ZOOM_CC_URL", "https://wmcc.zoom.us/closedcaption?id=9&signature=s")
    cfg = build_config(make_parser().parse_args(["run"]))
    assert cfg.zoom_cc.api_token_url.endswith("signature=s")


def test_host_flag(monkeypatch):
    cfg = build_config(make_parser().parse_args(["run", "--host", "0.0.0.0", "--port", "0"]))
    assert cfg.server.host == "0.0.0.0"
