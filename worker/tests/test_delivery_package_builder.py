from pathlib import Path

from app.pipeline.delivery_package_builder import DeliveryPackageBuilder


def test_delivery_package_builder_generates_studio_friendly_manifest():
    builder = DeliveryPackageBuilder()

    package = builder.build(
        job_id="job-123",
        clip_mode="short_serie",
        video_ratio="portrait",
        cuts=[
            {
                "start": 10.0,
                "end": 45.0,
                "title": "Titulo",
                "description": "Descricao",
                "hook": "Gancho",
                "hashtags": ["#a"],
                "thumbnail": "thumb",
                "merge_group": "story_1",
            }
        ],
        cut_files=[Path("/tmp/cut_01.mp4")],
        long_video_script={"title": "Longo"},
        qa_report={"decision": "needs_review"},
        artifacts_manifest={"artifacts": {"cut_01": {"storage_object": "jobs/job-123/cuts/cut_01.mp4"}}},
    )

    assert package["job_id"] == "job-123"
    assert package["delivery_status"] == "needs_review"
    assert package["clip_count"] == 1
    assert package["clips"][0]["file_name"] == "cut_01.mp4"
    assert package["long_video_script"]["title"] == "Longo"
