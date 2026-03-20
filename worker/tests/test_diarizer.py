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


def test_diarizer_falls_back_to_use_auth_token_when_token_kwarg_is_unsupported():
    class FakePipeline:
        calls = []

        @classmethod
        def from_pretrained(cls, model_name, **kwargs):
            cls.calls.append((model_name, kwargs))
            if "token" in kwargs:
                raise TypeError("unexpected keyword argument 'token'")
            return object()

    diarizer = SpeakerDiarizer(
        enabled=False,
        model_name="pyannote/speaker-diarization-3.1",
        hf_token="hf_test",
    )

    loaded = diarizer._load_pipeline(FakePipeline, "model-id", "hf_test")

    assert loaded is not None
    assert FakePipeline.calls[0][1] == {"token": "hf_test"}
    assert FakePipeline.calls[1][1] == {"use_auth_token": "hf_test"}
