from pathlib import Path

from app.video.qa import ClipQA


class StubClipQA(ClipQA):

    def __init__(self):
        super().__init__(min_duration_sec=25, max_duration_sec=90, max_speakers_per_clip=2)

    def _probe_duration(self, video_path: Path) -> float:
        return 40.0 if video_path.exists() else 0.0


def test_clip_qa_marks_clip_for_review_when_many_speakers(tmp_path):
    clip_file = tmp_path / "cut_01.mp4"
    clip_file.write_bytes(b"fake")

    qa = StubClipQA()
    report = qa.evaluate(
        requested_cuts=[
            {
                "start": 0.0,
                "end": 40.0,
                "hook": "gancho",
                "title": "titulo",
                "description": "descricao",
            }
        ],
        rendered_files=[clip_file],
        transcript_segments=[
            {"start": 0.0, "end": 10.0, "speaker": "SPEAKER_01"},
            {"start": 10.0, "end": 20.0, "speaker": "SPEAKER_02"},
            {"start": 20.0, "end": 30.0, "speaker": "SPEAKER_03"},
        ],
    )

    assert report["decision"] == "needs_review"
    assert report["clips"][0]["decision"] == "needs_review"
    assert report["clips"][0]["score"] < 100
    assert report["summary"]["average_score"] < 100


def test_clip_qa_blocks_invalid_render_duration(tmp_path):
    qa = StubClipQA()
    report = qa.evaluate(
        requested_cuts=[
            {
                "start": 0.0,
                "end": 20.0,
            }
        ],
        rendered_files=[tmp_path / "missing.mp4"],
        transcript_segments=[],
    )

    assert report["decision"] == "blocked"
    assert report["clips"][0]["decision"] == "blocked"
    assert report["clips"][0]["score"] <= 60


def test_clip_qa_penalizes_missing_and_unmeasurable_metadata(tmp_path):
    clip_file = tmp_path / "cut_01.mp4"
    clip_file.write_bytes(b"fake")

    # PR-CUT-01 changed two things here:
    #   * the generic_title/generic_thumbnail blocklists were literal outputs of one old
    #     geopolitics job; judging whether a title is generic is an editorial call for the
    #     model, not a hardcoded list, so those warnings are gone;
    #   * post metadata is read from `post_metadata`, not from the cut, and degraded
    #     diarization reports `speaker_continuity_unmeasurable` instead of claiming a pass.
    qa = StubClipQA()
    report = qa.evaluate(
        requested_cuts=[{"start": 0.0, "end": 40.0}],
        rendered_files=[clip_file],
        transcript_segments=[
            {"start": 0.0, "end": 40.0, "speaker": "UNKNOWN"},
        ],
        post_metadata={"hashtags": ["#dinheiro"]},
        diarization_status="degraded",
    )

    warnings = set(report["clips"][0]["warnings"])
    assert "missing_title" in warnings
    assert "missing_description" in warnings
    assert "sparse_hashtags" in warnings
    assert "speaker_continuity_unmeasurable" in warnings
    assert report["clips"][0]["score"] < 100
