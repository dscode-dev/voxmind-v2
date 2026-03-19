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
