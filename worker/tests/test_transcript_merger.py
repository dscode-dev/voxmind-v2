from app.media.transcript_merger import TranscriptSpeakerMerger


def test_transcript_merger_assigns_speaker_by_highest_overlap():
    segments = [
        {"start": 0.0, "end": 4.0, "text": "Primeira fala"},
        {"start": 4.0, "end": 8.0, "text": "Segunda fala"},
    ]
    speaker_turns = [
        {"speaker": "SPEAKER_01", "start": 0.0, "end": 3.5},
        {"speaker": "SPEAKER_02", "start": 3.5, "end": 8.0},
    ]

    merger = TranscriptSpeakerMerger(min_overlap_sec=0.2)
    merged = merger.merge(segments, speaker_turns)

    assert merged[0]["speaker"] == "SPEAKER_01"
    assert merged[1]["speaker"] == "SPEAKER_02"


def test_transcript_merger_falls_back_to_unknown_without_turns():
    segments = [{"start": 0.0, "end": 2.0, "text": "Sem diarizacao"}]

    merger = TranscriptSpeakerMerger()
    merged = merger.merge(segments, [])

    assert merged[0]["speaker"] == "UNKNOWN"
