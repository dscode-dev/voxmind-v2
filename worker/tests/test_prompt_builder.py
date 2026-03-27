from app.prompts.manual_prompt_builder import ManualPromptBuilder
from app.pipeline.pipeline import Pipeline


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
    assert '"final_videos"' in prompt
    assert '"title"' in prompt
    assert '"description"' in prompt
    assert '"hashtags"' in prompt
    assert '"thumbnail"' in prompt
    assert '"soundtrack_suggestion"' in prompt
    assert '"safe_start"' in prompt
    assert '"safe_end"' in prompt
    assert '"speaker_focus"' in prompt
    assert '"transition_after"' in prompt
    assert "CANDIDATOS PRIORIZADOS" not in prompt


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


def test_manual_prompt_builder_adapts_when_speakers_are_unknown():
    transcript = [
        {"start": 0.0, "end": 12.0, "text": "Bloco inicial de contexto.", "speaker": "UNKNOWN"},
        {"start": 12.0, "end": 24.0, "text": "Fechamento da ideia principal.", "speaker": "UNKNOWN"},
    ]
    candidates = [
        {
            "candidate_id": "cand_0001",
            "start": 0.0,
            "end": 24.0,
            "duration": 24.0,
            "text": "Bloco inicial de contexto. Fechamento da ideia principal.",
            "total_score": 12.0,
            "speakers": ["UNKNOWN"],
            "score_breakdown": {"hook_score": 3.0},
        }
    ]

    prompt = ManualPromptBuilder(max_context_chars=2000).build(
        transcript=transcript,
        candidates=candidates,
        job_id="job-789",
        clip_mode="short",
        video_ratio="portrait",
    )

    assert "REGRAS SOBRE LOCUTOR" in prompt
    assert "Não invente trocas de locutor." in prompt
    assert "REGRAS ESPECÍFICAS DE LOCUTOR" not in prompt


def test_pipeline_parse_manual_response_tolerates_smart_quotes_and_code_fences():
    pipeline = Pipeline(
        video_url="https://example.com/video",
        job_id="job-json-fix",
        manual_response={},
    )

    payload = """```json
{
“job_id”: “job-json-fix”,
“shorts_content”: [
  {
    “start”: 10.0,
    “end”: 40.0,
    “title”: “Teste”,
    “hook”: “Gancho suficiente para validar”,
    “merge_group”: “story_1”,
  }
],
}
```"""

    parsed = pipeline._parse_manual_response(payload)

    assert parsed["job_id"] == "job-json-fix"
    assert parsed["shorts_content"][0]["title"] == "Teste"


def test_pipeline_parse_manual_response_tolerates_single_quotes_and_json_like_literals():
    pipeline = Pipeline(
        video_url="https://example.com/video",
        job_id="job-json-like-fix",
        manual_response={},
    )

    payload = """{
  'job_id': 'job-json-like-fix',
  'final_videos': [
    {
      'video_index': 1,
      'title': 'Teste',
      'hook': 'Quem decide esse tipo de coisa?',
      'description': null,
      'hashtags': ['#tag1'],
      'thumbnail': 'thumbnail',
      'soundtrack_suggestion': 'political_tension',
      'speaker_focus': null,
      'shorts_content': [
        {
          'start': 10.0,
          'end': 40.0,
          'merge_group': 'story_1',
          'transition_after': 'fade'
        }
      ]
    }
  ],
  'approved': true
}"""

    parsed = pipeline._parse_manual_response(payload)

    assert parsed["job_id"] == "job-json-like-fix"
    assert parsed["final_videos"][0]["title"] == "Teste"
    assert parsed["final_videos"][0]["description"] is None
