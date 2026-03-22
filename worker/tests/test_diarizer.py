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


def test_diarizer_uses_token_parameter_when_supported():
    class FakePipeline:
        calls = []

        @classmethod
        def from_pretrained(cls, model_name, token=None):
            cls.calls.append((model_name, {"token": token}))
            return object()

    diarizer = SpeakerDiarizer(
        enabled=False,
        model_name="pyannote/speaker-diarization-3.1",
        hf_token="hf_test",
    )

    loaded = diarizer._load_pipeline(FakePipeline, "model-id", "hf_test")

    assert loaded is not None
    assert FakePipeline.calls[0][1] == {"token": "hf_test"}


def test_diarizer_uses_use_auth_token_when_token_parameter_is_unavailable():
    class FakePipeline:
        calls = []

        @classmethod
        def from_pretrained(cls, model_name, use_auth_token=None):
            cls.calls.append((model_name, {"use_auth_token": use_auth_token}))
            return object()

    diarizer = SpeakerDiarizer(
        enabled=False,
        model_name="pyannote/speaker-diarization-3.1",
        hf_token="hf_test",
    )

    loaded = diarizer._load_pipeline(FakePipeline, "model-id", "hf_test")

    assert loaded is not None
    assert FakePipeline.calls[0][1] == {"use_auth_token": "hf_test"}
