from pathlib import Path
from typing import Dict, List


class DeliveryPackageBuilder:

    def build(
        self,
        job_id: str,
        clip_mode: str,
        video_ratio: str,
        cuts: List[Dict],
        cut_files: List[Path],
        long_video_script: Dict | None,
        qa_report: Dict | None,
        automation_report: Dict | None,
        artifacts_manifest: Dict | None,
    ) -> Dict:
        clips = []
        automation_by_index = {
            int(clip.get("clip_index", 0)): clip
            for clip in (automation_report or {}).get("clips", [])
            if clip.get("clip_index")
        }

        for index, cut_file in enumerate(cut_files):
            cut = cuts[index] if index < len(cuts) else {}
            clip_index = index + 1
            clips.append(
                {
                    "clip_index": clip_index,
                    "file_name": cut_file.name,
                    "local_path": str(cut_file),
                    "start": float(cut.get("start", 0.0)),
                    "end": float(cut.get("end", 0.0)),
                    "duration": max(
                        0.0,
                        float(cut.get("end", 0.0)) - float(cut.get("start", 0.0)),
                    ),
                    "title": cut.get("title"),
                    "description": cut.get("description"),
                    "hook": cut.get("hook"),
                    "hashtags": cut.get("hashtags", []),
                    "thumbnail": cut.get("thumbnail"),
                    "merge_group": cut.get("merge_group"),
                    "automation": automation_by_index.get(clip_index),
                }
            )

        return {
            "job_id": job_id,
            "clip_mode": clip_mode,
            "video_ratio": video_ratio,
            "delivery_status": self._resolve_delivery_status(qa_report),
            "qa_decision": qa_report.get("decision") if qa_report else None,
            "automation": automation_report,
            "clip_count": len(clips),
            "clips": clips,
            "long_video_script": long_video_script,
            "artifacts_manifest": artifacts_manifest or {},
        }

    def _resolve_delivery_status(self, qa_report: Dict | None) -> str:
        if not qa_report:
            return "ready"

        decision = qa_report.get("decision")
        if decision == "blocked":
            return "blocked"
        if decision == "needs_review":
            return "needs_review"
        return "ready"
