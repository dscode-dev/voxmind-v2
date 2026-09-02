"""Structural AI contract, schema authority and bounded repair (PR-CUT-01).

No network: the provider is always a local callable.
"""

import json
from pathlib import Path

import pytest

from app.ai.schemas import CutModel, CutsResponseModel, FinalVideoModel, json_schema
from app.ai.validation import (
    AIResponseValidationError,
    generate_validated_cuts,
    validate_cuts_response,
    validate_span_grounding,
)


VALID = {
    "job_id": "job-1",
    "final_videos": [
        {
            "video_index": 1,
            "hook_id": "hook_0001",
            "span_ids": ["span_0003", "span_0004"],
            "title": "Titulo",
            "hook": "um gancho suficientemente longo para o teste",
            "hook_start": 10.5,
            "hook_end": 16.8,
            "description": "descricao",
            "hashtags": ["#a", "#b", "#c"],
            "shorts_content": [
                {"start": 10.5, "end": 45.3, "reason": "ok", "narrative_role": "hook"}
            ],
        }
    ],
}


# ==========================================================================
# Case E — invalid AI output never reaches the normalizer
# ==========================================================================


def test_valid_response_passes():
    assert validate_cuts_response(VALID)["job_id"] == "job-1"


@pytest.mark.parametrize(
    "payload,expected",
    [
        ("not a dict", "JSON object"),
        ({}, "final_videos"),
        ({"final_videos": []}, "final_videos"),
        ({"final_videos": [{"video_index": 1}]}, "selection"),
    ],
)
def test_structurally_invalid_payloads_are_rejected(payload, expected):
    with pytest.raises(AIResponseValidationError) as exc:
        validate_cuts_response(payload)
    assert expected in str(exc.value)


def test_inverted_timestamps_are_rejected():
    bad = json.loads(json.dumps(VALID))
    bad["final_videos"][0]["shorts_content"][0] = {"start": 60.0, "end": 30.0}
    with pytest.raises(AIResponseValidationError, match="greater than start"):
        validate_cuts_response(bad)


def test_non_finite_timestamps_are_rejected():
    bad = json.loads(json.dumps(VALID))
    bad["final_videos"][0]["shorts_content"][0] = {"start": float("nan"), "end": 30.0}
    with pytest.raises(AIResponseValidationError):
        validate_cuts_response(bad)


def test_negative_timestamps_are_rejected():
    bad = json.loads(json.dumps(VALID))
    bad["final_videos"][0]["shorts_content"][0] = {"start": -5.0, "end": 30.0}
    with pytest.raises(AIResponseValidationError, match="negative"):
        validate_cuts_response(bad)


def test_unknown_narrative_role_is_rejected():
    bad = json.loads(json.dumps(VALID))
    bad["final_videos"][0]["shorts_content"][0]["narrative_role"] = "climax"
    with pytest.raises(AIResponseValidationError, match="narrative_role"):
        validate_cuts_response(bad)


def test_inverted_hook_window_is_rejected():
    bad = json.loads(json.dumps(VALID))
    bad["final_videos"][0]["hook_start"] = 20.0
    bad["final_videos"][0]["hook_end"] = 10.0
    with pytest.raises(AIResponseValidationError, match="hook_end"):
        validate_cuts_response(bad)


def test_span_ids_only_selection_is_valid():
    """Selecting purely by span_ids is the preferred shape."""
    payload = {"final_videos": [{"video_index": 1, "span_ids": ["span_0001", "span_0002"]}]}
    assert validate_cuts_response(payload)["final_videos"]


def test_the_prompts_pipe_separated_placeholder_is_tolerated():
    """A model echoing 'hook | setup | payoff' is a formatting slip, not a structural fault."""
    payload = json.loads(json.dumps(VALID))
    payload["final_videos"][0]["shorts_content"][0]["narrative_role"] = "hook | setup | payoff"
    assert validate_cuts_response(payload)


def test_structural_validity_does_not_judge_editorial_quality():
    """An 11s cut is below the short preset's editorial minimum but structurally fine —
    Pydantic must not silently overrule the preset."""
    payload = json.loads(json.dumps(VALID))
    payload["final_videos"][0]["shorts_content"][0] = {"start": 0.0, "end": 11.0}
    assert validate_cuts_response(payload)


def test_unknown_keys_survive_validation():
    payload = json.loads(json.dumps(VALID))
    payload["_custom"] = {"kept": True}
    assert validate_cuts_response(payload)["_custom"] == {"kept": True}


# ==========================================================================
# Bounded repair
# ==========================================================================


def test_valid_first_response_makes_no_repair_call():
    calls = []

    def generate(system, user):
        calls.append(user)
        return VALID

    result, stats = generate_validated_cuts(generate, "sys", "user")

    assert len(calls) == 1
    assert stats == {"attempts": 1, "valid": True, "repair_attempted": False,
                     "repair_success": False, "errors": []}
    assert result["job_id"] == "job-1"


def test_one_repair_attempt_recovers_an_invalid_response():
    responses = [{"final_videos": []}, VALID]
    prompts = []

    def generate(system, user):
        prompts.append(user)
        return responses.pop(0)

    result, stats = generate_validated_cuts(generate, "sys", "user")

    assert stats["attempts"] == 2
    assert stats["repair_attempted"] is True
    assert stats["repair_success"] is True
    assert "CORRECTION REQUIRED" in prompts[1]
    assert result["job_id"] == "job-1"


def test_repair_is_bounded_at_one_attempt():
    """Never a loop: two calls maximum, then the job fails deterministically."""
    calls = []

    def generate(system, user):
        calls.append(user)
        return {"final_videos": []}

    with pytest.raises(AIResponseValidationError):
        generate_validated_cuts(generate, "sys", "user")

    assert len(calls) == 2


def test_repair_prompt_carries_the_actual_errors():
    prompts = []

    def generate(system, user):
        prompts.append(user)
        return VALID if len(prompts) > 1 else {"final_videos": [{"video_index": 1}]}

    generate_validated_cuts(generate, "sys", "original prompt")

    assert "original prompt" in prompts[1]
    assert "selection" in prompts[1]


def test_repair_events_are_emitted():
    emitted = []
    responses = [{"final_videos": []}, VALID]

    generate_validated_cuts(
        lambda s, u: responses.pop(0),
        "sys",
        "user",
        emit=lambda name, **kw: emitted.append(name),
    )

    assert "AI_VALIDATION_FAILED" in emitted
    assert "AI_REPAIR_ATTEMPTED" in emitted
    assert "AI_REPAIR_SUCCESS" in emitted


# ==========================================================================
# Span grounding check
# ==========================================================================


def test_referenced_spans_outside_the_offered_set_are_reported():
    blind = validate_span_grounding(VALID, {"span_0003"})
    assert blind == ["span_0004"]


def test_no_blind_references_when_everything_was_offered():
    assert validate_span_grounding(VALID, {"span_0003", "span_0004"}) == []


# ==========================================================================
# §15 — one schema authority
# ==========================================================================


def test_checked_in_schema_matches_the_pydantic_models():
    """cuts_schema.json is generated from app/ai/schemas.py. If this fails, regenerate it."""
    path = Path(__file__).resolve().parents[1] / "app/prompts/schemas/cuts_schema.json"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    generated = json_schema()

    for key in ("properties", "$defs", "required", "type"):
        assert on_disk.get(key) == generated.get(key), (
            f"cuts_schema.json drifted from the Pydantic models at {key!r}. "
            "Regenerate it from app.ai.schemas.json_schema()."
        )


def test_schema_declares_it_is_generated():
    path = Path(__file__).resolve().parents[1] / "app/prompts/schemas/cuts_schema.json"
    assert "GENERATED FILE" in path.read_text(encoding="utf-8")


def test_models_expose_the_selection_helpers():
    model = CutsResponseModel.model_validate(VALID)
    assert model.referenced_span_ids() == {"span_0003", "span_0004"}
    assert len(model.all_cuts()) == 1
    assert isinstance(model.final_videos[0], FinalVideoModel)
    assert isinstance(model.all_cuts()[0], CutModel)


# ==========================================================================
# §17 — manual mode converges on the same structural contract
# ==========================================================================


def test_manual_response_uses_the_same_structural_gate():
    """A manual `response.json` used to reach the normalizer having only been JSON-parsed.
    It now passes the identical contract the automatic path uses."""
    from app.pipeline import pipeline as pipeline_module

    source = Path(pipeline_module.__file__).read_text(encoding="utf-8")
    finalize = source.split("def _finalize_stage")[1].split("def ")[0]

    assert "validate_cuts_response(" in finalize
    # The gate runs before any normalization call.
    gate = finalize.index("validate_cuts_response(")
    for normalizer in ("_expand_response_from_span_ids", "_normalize_response_schema"):
        assert normalizer not in finalize[:gate], (
            f"{normalizer} runs before the structural gate"
        )


def test_manual_and_automatic_share_one_validator():
    import app.ai.validation as validation

    assert validation.validate_cuts_response.__module__ == "app.ai.validation"
    # One definition, used by both paths — not two divergent rule sets.
    assert validation.CutsResponseModel is CutsResponseModel


@pytest.mark.parametrize(
    "manual_payload",
    [
        {"shorts_content": [{"start": 30.0, "end": 10.0}]},   # inverted
        {"shorts_content": []},                                # empty
        {"final_videos": [{"video_index": 1}]},                # no selection
    ],
)
def test_invalid_manual_payloads_are_rejected_before_normalization(manual_payload):
    with pytest.raises(AIResponseValidationError):
        validate_cuts_response(manual_payload)
