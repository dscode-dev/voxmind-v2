from app.media.diarizer import SpeakerDiarizer


def test_diarizer_reports_missing_hf_token():
    diarizer = SpeakerDiarizer(
        enabled=True,
        model_name="pyannote/speaker-diarization-3.1",
        hf_token=None,
    )

    diagnostics = diarizer.diagnostics()

    assert diagnostics["enabled"] is True
    assert diagnostics["token_present"] is False
    assert diagnostics["available"] is False
    assert diagnostics["availability_reason"] == "missing_hf_token"
