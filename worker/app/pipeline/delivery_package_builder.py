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
        final_clip_files: List[Path],
        final_reel_path: Path | None,
        subtitle_path: Path | None,
        post_payload: Dict | None,
        long_video_script: Dict | None,
        qa_report: Dict | None,
        automation_report: Dict | None,
        render_plan: Dict | None,
        artifacts_manifest: Dict | None,
        response_validation: Dict | None = None,
        final_video_specs: List[Dict] | None = None,
    ) -> Dict:
        clips = []
        videos = []
        automation_by_index = {
            int(clip.get("clip_index", 0)): clip
            for clip in (automation_report or {}).get("clips", [])
            if clip.get("clip_index")
        }

        for index, cut_file in enumerate(cut_files):
            cut = cuts[index] if index < len(cuts) else {}
            clip_index = index + 1
            final_clip_file = final_clip_files[index] if index < len(final_clip_files) else None
            clips.append(
                {
                    "clip_index": clip_index,
                    "file_name": cut_file.name,
                    "local_path": str(cut_file),
                    "final_file_name": final_clip_file.name if final_clip_file else None,
                    "final_local_path": str(final_clip_file) if final_clip_file else None,
                    "start": float(cut.get("start", 0.0)),
                    "end": float(cut.get("end", 0.0)),
                    "safe_start": float(cut.get("safe_start", cut.get("start", 0.0))),
                    "safe_end": float(cut.get("safe_end", cut.get("end", 0.0))),
                    "duration": max(
                        0.0,
                        float(cut.get("end", 0.0)) - float(cut.get("start", 0.0)),
                    ),
                    "merge_group": cut.get("merge_group"),
                    "speaker_focus": cut.get("speaker_focus"),
                    "transition_after": cut.get("transition_after"),
                    "automation": automation_by_index.get(clip_index),
                }
            )
        if final_video_specs:
            for index, spec in enumerate(final_video_specs, start=1):
                final_clip_file = final_clip_files[index - 1] if index - 1 < len(final_clip_files) else None
                videos.append(
                    {
                        "video_index": int(spec.get("video_index") or index),
                        "post": spec.get("post") or post_payload or {},
                        "clip_count": len(spec.get("cuts") or []),
                        "final_file_name": final_clip_file.name if final_clip_file else None,
                        "final_local_path": str(final_clip_file) if final_clip_file else None,
                    }
                )
        else:
            for index, cut in enumerate(cuts, start=1):
                final_clip_file = final_clip_files[index - 1] if index - 1 < len(final_clip_files) else None
                videos.append(
                    {
                        "video_index": index,
                        "post": cut.get("_post") or post_payload or {},
                        "clip_index": index,
                        "final_file_name": final_clip_file.name if final_clip_file else None,
                        "final_local_path": str(final_clip_file) if final_clip_file else None,
                    }
                )

        return {
            "job_id": job_id,
            "clip_mode": clip_mode,
            "video_ratio": video_ratio,
            "delivery_status": self._resolve_delivery_status(qa_report),
            "qa_decision": qa_report.get("decision") if qa_report else None,
            "response_validation": response_validation or {},
            "automation": automation_report,
            "post": post_payload or {},
            "videos": videos,
            "clip_count": len(clips),
            "clips": clips,
            "final_assets": {
                "final_clips": self._final_clips_payload(final_clip_files),
                "final_reel": self._final_reel_payload(final_reel_path),
                "subtitles": self._subtitle_payload(subtitle_path),
            },
            "long_video_script": long_video_script,
            "render_plan": render_plan or {},
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

    def _final_reel_payload(self, final_reel_path: Path | None) -> Dict:
        if final_reel_path is None:
            return {
                "status": "not_generated",
                "file_name": None,
                "local_path": None,
            }

        return {
            "status": "generated" if final_reel_path.exists() else "missing",
            "file_name": final_reel_path.name,
            "local_path": str(final_reel_path),
        }

    def _subtitle_payload(self, subtitle_path: Path | None) -> Dict:
        if subtitle_path is None:
            return {
                "status": "not_generated",
                "file_name": None,
                "local_path": None,
                "format": "srt",
            }

        return {
            "status": "generated" if subtitle_path.exists() else "missing",
            "file_name": subtitle_path.name,
            "local_path": str(subtitle_path),
            "format": "srt",
        }

    def _final_clips_payload(self, final_clip_files: List[Path]) -> List[Dict]:
        payload: List[Dict] = []
        for index, path in enumerate(final_clip_files, start=1):
            payload.append(
                {
                    "clip_index": index,
                    "status": "generated" if path.exists() else "missing",
                    "file_name": path.name,
                    "local_path": str(path),
                }
            )
        return payload
