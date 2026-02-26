from __future__ import annotations

from voxmind.app.utils.transcript_utils import TranscriptChunk


def build_candidate_prompt(chunks: list[TranscriptChunk]) -> str:
    return "\n\n".join([f"[chunk {c.index}]\n{c.text}" for c in chunks])
