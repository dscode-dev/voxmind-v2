from voxmind.app.utils.transcript_utils import chunk_transcript, TranscriptChunk

def make_chunks(transcript: str, *, max_chars: int = 2500) -> list[TranscriptChunk]:
    return chunk_transcript(transcript, max_chars=max_chars)
