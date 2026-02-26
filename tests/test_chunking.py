from voxmind.app.utils.transcript_utils import chunk_transcript


def test_chunking_non_empty():
    t = "a\n" * 1000
    chunks = chunk_transcript(t, max_chars=100)
    assert len(chunks) > 1
