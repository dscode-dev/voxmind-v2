"""Structural gate for AI (and manual) cut responses.

Previously this module accepted any dict containing one non-empty list, so a response with
inverted timestamps, NaNs, or string coordinates flowed straight into ~2,800 lines of
normalization heuristics that assumed well-formed numbers.

The gate is now the Pydantic contract in ``app.ai.schemas``, and it sits in front of the
normalizer on **both** paths — automatic and manual — so there is one structural definition
rather than two.

Repair is bounded and explicit:

    attempt 1 → invalid → one repair request → attempt 2 → valid | fail

There is no loop. Two provider calls at most.
"""
from __future__ import annotations

from typing import Any, Callable

from pydantic import ValidationError

from app.ai.schemas import CutsResponseModel, RawEditResponseModel
from app.observability import get_logger

logger = get_logger(__name__)


class AIResponseValidationError(ValueError):
    """The response is not structurally usable. Deterministic: retrying the job will not fix it."""

    def __init__(self, message: str, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []


MAX_REPAIR_ATTEMPTS = 1


def format_validation_errors(error: ValidationError, limit: int = 8) -> str:
    """A compact, model-readable description of what was wrong."""
    lines = []
    for item in error.errors()[:limit]:
        location = ".".join(str(part) for part in item.get("loc", ()))
        lines.append(f"- {location or '<root>'}: {item.get('msg')}")
    remaining = len(error.errors()) - limit
    if remaining > 0:
        lines.append(f"- ... and {remaining} more")
    return "\n".join(lines)


def validate_cuts_response(data: Any, *, is_raw_edit: bool = False) -> dict[str, Any]:
    """Validate structurally and return the normalized dict.

    Raises AIResponseValidationError when the payload cannot be used.
    """
    if not isinstance(data, dict):
        raise AIResponseValidationError("AI response must be a JSON object")

    model_cls = RawEditResponseModel if is_raw_edit else CutsResponseModel

    try:
        model = model_cls.model_validate(data)
    except ValidationError as exc:
        raise AIResponseValidationError(
            f"AI response failed structural validation:\n{format_validation_errors(exc)}",
            errors=exc.errors(),
        ) from exc

    # Preserve unknown keys the normalizer may rely on, with validated values on top.
    validated = {**data, **model.model_dump(exclude_none=True, exclude_defaults=False)}
    return validated


def validate_span_grounding(
    data: dict[str, Any],
    selectable_span_ids: set[str],
) -> list[str]:
    """Return referenced span ids that were never shown to the model.

    A non-empty result means the request violated the grounding invariant; the caller
    decides what to do. Reported rather than raised so an otherwise-usable selection is not
    thrown away.
    """
    try:
        model = CutsResponseModel.model_validate(data)
    except ValidationError:
        return []

    referenced = model.referenced_span_ids()
    return sorted(referenced - set(selectable_span_ids))


def build_repair_prompt(original_user_prompt: str, error: AIResponseValidationError) -> str:
    """One corrective instruction, carrying the exact structural failures."""
    return (
        f"{original_user_prompt}\n\n"
        "=== CORRECTION REQUIRED ===\n"
        "Your previous response was rejected because it did not satisfy the required "
        "structure:\n"
        f"{error}\n\n"
        "Return ONLY corrected, valid JSON matching the schema above. Do not explain the "
        "correction. Keep the same editorial selection where it was valid; fix only what "
        "the errors above describe."
    )


def generate_validated_cuts(
    generate: Callable[[str, str], Any],
    system_prompt: str,
    user_prompt: str,
    *,
    is_raw_edit: bool = False,
    emit: Callable[..., None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Call the provider, validate, and repair once if needed.

    ``generate(system_prompt, user_prompt)`` performs one provider call.

    Returns (validated_response, stats) where stats records the attempt accounting the
    evaluation harness reports on.
    """
    stats = {
        "attempts": 0,
        "valid": False,
        "repair_attempted": False,
        "repair_success": False,
        "errors": [],
    }

    def _emit(name: str, **fields):
        if emit is not None:
            try:
                emit(name, **fields)
            except Exception:
                pass

    stats["attempts"] += 1
    raw = generate(system_prompt, user_prompt)

    try:
        validated = validate_cuts_response(raw, is_raw_edit=is_raw_edit)
        stats["valid"] = True
        return validated, stats
    except AIResponseValidationError as exc:
        # Python unbinds the `as` name when the except block exits; keep a reference.
        first_error = exc
        stats["errors"].append(str(first_error))
        logger.warning(
            "AI response failed structural validation; attempting one bounded repair",
            extra={"step": "ai_validation", "status": "failed"},
        )
        _emit("AI_VALIDATION_FAILED", error=str(first_error))

    stats["repair_attempted"] = True
    _emit("AI_REPAIR_ATTEMPTED")
    stats["attempts"] += 1

    repair_prompt = build_repair_prompt(user_prompt, first_error)
    repaired_raw = generate(system_prompt, repair_prompt)

    try:
        validated = validate_cuts_response(repaired_raw, is_raw_edit=is_raw_edit)
    except AIResponseValidationError as second_error:
        stats["errors"].append(str(second_error))
        logger.error(
            "AI repair attempt also failed structural validation",
            extra={"step": "ai_validation", "status": "failed"},
        )
        _emit("AI_REPAIR_FAILED", error=str(second_error))
        # Deterministic: the queue classifies this non-retryable rather than burning attempts.
        raise

    stats["valid"] = True
    stats["repair_success"] = True
    logger.info(
        "AI repair attempt produced a structurally valid response",
        extra={"step": "ai_validation", "status": "repaired"},
    )
    _emit("AI_REPAIR_SUCCESS")
    return validated, stats
