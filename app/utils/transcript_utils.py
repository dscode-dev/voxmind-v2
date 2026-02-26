from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TranscriptChunk:
    index: int
    text: str


def chunk_transcript(transcript: str, *, max_chars: int = 2500) -> list[TranscriptChunk]:
    """MVP chunking by character size. We'll upgrade to timestamp-aware chunking later."""
    transcript = transcript.strip()
    if not transcript:
        return []

    chunks: list[TranscriptChunk] = []
    buf: list[str] = []
    cur = 0
    idx = 0

    for line in transcript.splitlines():
        if cur + len(line) + 1 > max_chars and buf:
            chunks.append(TranscriptChunk(index=idx, text="\n".join(buf).strip()))
            idx += 1
            buf = []
            cur = 0
        buf.append(line)
        cur += len(line) + 1

    if buf:
        chunks.append(TranscriptChunk(index=idx, text="\n".join(buf).strip()))
    return chunks
