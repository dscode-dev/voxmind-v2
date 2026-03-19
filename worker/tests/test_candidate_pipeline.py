from app.pipeline.candidate_builder import CandidateBuilder
from app.pipeline.scorer import Scorer


def test_candidate_builder_limits_candidates_per_time_window():
    chunks = []

    for index in range(6):
        chunks.append(
            {
                "start": float(index * 20),
                "end": float(index * 20 + 40),
                "text": f"Esse trecho {index} tem revelacao forte e termina com conclusao.",
                "hook_score": 4,
                "audio_peak_score": 0.8,
                "story_setup": 1,
                "story_conflict": 1,
                "story_reveal": 2,
                "segment_count": 3,
                "speaker_count": 1,
                "speakers": ["SPEAKER_01"],
            }
        )

    builder = CandidateBuilder(
        min_total_score=5.0,
        max_candidates_per_window=2,
        window_size_sec=120,
    )

    candidates = builder.build(chunks)

    assert len(candidates) == 2
    assert all(candidate["window_index"] == 0 for candidate in candidates)


def test_scorer_suppresses_heavily_overlapping_candidates():
    candidates = [
        {
            "candidate_id": "cand_1",
            "start": 0.0,
            "end": 50.0,
            "duration": 50.0,
            "text": "Primeiro candidato",
            "total_score": 15.0,
            "window_index": 0,
            "boundary_score": 2.0,
            "speaker_count": 1,
            "speakers": ["SPEAKER_01"],
        },
        {
            "candidate_id": "cand_2",
            "start": 5.0,
            "end": 48.0,
            "duration": 43.0,
            "text": "Segundo candidato quase igual",
            "total_score": 14.8,
            "window_index": 0,
            "boundary_score": 1.5,
            "speaker_count": 1,
            "speakers": ["SPEAKER_01"],
        },
        {
            "candidate_id": "cand_3",
            "start": 130.0,
            "end": 175.0,
            "duration": 45.0,
            "text": "Terceiro candidato distante",
            "total_score": 13.0,
            "window_index": 1,
            "boundary_score": 2.0,
            "speaker_count": 1,
            "speakers": ["SPEAKER_01"],
        },
    ]

    scorer = Scorer(
        max_candidates=10,
        max_candidates_per_window=2,
        min_start_gap=10,
        overlap_iou_threshold=0.5,
    )

    ranked = scorer.score(candidates)

    assert len(ranked) == 2
    assert [candidate["candidate_id"] for candidate in ranked] == ["cand_1", "cand_3"]
