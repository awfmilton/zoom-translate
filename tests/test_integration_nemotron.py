"""Real Nemotron + ct2 MT tests. Skipped unless ZOOMLT_INTEGRATION=1 (downloads ~750 MB on first run)."""

from zoomlt.config import AsrConfig, MtConfig


def test_nemotron_transcribes_spanish_and_hindi(integration, es_audio, hi_audio):
    from zoomlt.asr_nemotron import NemotronEngine

    eng = NemotronEngine(AsrConfig(engine="nemotron", languages=["es", "hi"], mode="dual"))
    es = eng.transcribe(es_audio, task="transcribe", utterance_id=1)
    assert es.language == "es" and "equipos" in es.text.lower(), es.text
    hi = eng.transcribe(hi_audio, task="transcribe", utterance_id=2)
    assert hi.language == "hi" and "महिलाओं" in hi.text, hi.text
    # incremental partial then final on the same utterance
    half = es_audio[: len(es_audio) // 2]
    p = eng.transcribe(half, task="transcribe", is_partial=True, utterance_id=3)
    f = eng.transcribe(es_audio, task="transcribe", is_partial=False, utterance_id=3)
    assert len(f.text) >= len(p.text) and "equipos" in f.text.lower()


def test_ct2_translates_hindi_and_spanish(integration):
    from zoomlt.mt import Ct2Translator

    tr = Ct2Translator(MtConfig(engine="ct2"))
    assert tr.supports("es") and tr.supports("hi")
    es = tr.translate("Mañana discutiremos el presupuesto del próximo trimestre.", "es")
    hi = tr.translate("हम कल सुबह दस बजे बजट पर चर्चा करेंगे।", "hi")
    assert es and "budget" in es.lower(), es
    assert hi and "budget" in hi.lower(), hi
