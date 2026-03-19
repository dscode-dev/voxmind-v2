from app.pipeline.chunker import Chunker


def test_chunker_always_advances_and_generates_chunks():
    segments = [
        {"start": 0.0, "end": 8.0, "text": "Hoje eu vou te explicar como isso funciona"},
        {"start": 8.0, "end": 16.0, "text": "e por que quase ninguem percebe esse detalhe"},
        {"start": 16.0, "end": 28.0, "text": "mas no final o resultado aparece."},
        {"start": 28.0, "end": 40.0, "text": "Agora eu vou mostrar outro caso"},
        {"start": 40.0, "end": 54.0, "text": "que termina com uma conclusao bem clara."},
        {"start": 54.0, "end": 66.0, "text": "Por isso vale prestar atencao."},
    ]

    chunker = Chunker(
        min_duration=20,
        target_duration=30,
        max_duration=45,
        overlap=5,
    )

    chunks = chunker.chunk(segments)

    assert chunks
    assert all(chunks[index]["start"] < chunks[index + 1]["start"] for index in range(len(chunks) - 1))
    assert all(chunk["end"] > chunk["start"] for chunk in chunks)
    assert all(chunk["segment_count"] >= 1 for chunk in chunks)


def test_chunker_preserves_speaker_metadata_when_available():
    segments = [
        {"start": 0.0, "end": 12.0, "text": "Primeira fala", "speaker": "SPEAKER_01"},
        {"start": 12.0, "end": 24.0, "text": "Segunda fala", "speaker": "SPEAKER_02"},
        {"start": 24.0, "end": 36.0, "text": "No final tudo fecha.", "speaker": "SPEAKER_01"},
    ]

    chunker = Chunker(min_duration=20, target_duration=25, max_duration=40, overlap=4)

    chunks = chunker.chunk(segments)

    assert chunks[0]["speaker_count"] == 2
    assert chunks[0]["speakers"] == ["SPEAKER_01", "SPEAKER_02"]
