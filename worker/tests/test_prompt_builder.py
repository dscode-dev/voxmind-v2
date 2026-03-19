from app.prompts.manual_prompt_builder import ManualPromptBuilder


def test_manual_prompt_builder_includes_speakers_and_mode_rules():
    transcript = [
        {"start": 0.0, "end": 4.0, "text": "Primeira fala", "speaker": "SPEAKER_01"},
        {"start": 4.0, "end": 8.0, "text": "Resposta final.", "speaker": "SPEAKER_02"},
    ]
    candidates = [
        {
            "candidate_id": "cand_0001",
            "start": 0.0,
            "end": 8.0,
            "duration": 8.0,
            "text": "Primeira fala Resposta final.",
            "total_score": 12.0,
            "speakers": ["SPEAKER_01", "SPEAKER_02"],
            "score_breakdown": {"hook_score": 3.0},
        }
    ]

    prompt = ManualPromptBuilder(max_context_chars=2000).build(
        transcript=transcript,
        candidates=candidates,
        job_id="job-123",
        clip_mode="short_serie",
        video_ratio="portrait",
    )

    assert "SPEAKER_01" in prompt
    assert "SPEAKER_02" in prompt
    assert "MODO: SHORT_SERIE" in prompt
    assert "TRANSCRIPT RELEVANTE COM SPEAKERS" in prompt


def test_manual_prompt_builder_respects_context_budget():
    transcript = []
    for index in range(50):
        transcript.append(
            {
                "start": float(index * 4),
                "end": float(index * 4 + 4),
                "text": f"Trecho muito longo numero {index} com bastante contexto narrativo.",
                "speaker": "SPEAKER_01",
            }
        )

    candidates = [
        {
            "candidate_id": "cand_0001",
            "start": 40.0,
            "end": 60.0,
            "duration": 20.0,
            "text": "Trecho central importante.",
            "total_score": 14.0,
            "speakers": ["SPEAKER_01"],
            "score_breakdown": {"hook_score": 3.0},
        }
    ]

    prompt = ManualPromptBuilder(max_context_chars=900).build(
        transcript=transcript,
        candidates=candidates,
        job_id="job-456",
    )

    assert len(prompt) < 5000
