"""Lightweight validation of the AI cuts response.

This is the fast structural gate right after generation — it guarantees the payload is a dict
carrying a selection. The deep editorial normalization/validation still happens in the existing
finalize stage (``pipeline._finalize_stage``), which is left untouched.
"""
from __future__ import annotations

from typing import Any


class AIResponseValidationError(ValueError):
    pass


def validate_cuts_response(data: Any, *, is_raw_edit: bool = False) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise AIResponseValidationError("AI response must be a JSON object")

    if is_raw_edit:
        # Raw authorial edit has a different schema; presence of a dict is enough here.
        return data

    final_videos = data.get("final_videos")
    shorts_content = data.get("shorts_content")

    has_final_videos = isinstance(final_videos, list) and len(final_videos) > 0
    has_shorts = isinstance(shorts_content, list) and len(shorts_content) > 0

    if not has_final_videos and not has_shorts:
        raise AIResponseValidationError(
            "AI response must contain a non-empty 'final_videos' or 'shorts_content'"
        )

    return data
