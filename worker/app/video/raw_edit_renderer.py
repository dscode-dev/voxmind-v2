from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


class RawEditRenderer:
    def __init__(self, work_dir: Path):
        self.work_dir = work_dir
        self.render_dir = work_dir / "raw_edit_render"
        self.render_dir.mkdir(parents=True, exist_ok=True)

    def render(
        self,
        *,
        source_video: Path,
        edit_response: dict[str, Any],
        output_path: Path | None = None,
    ) -> tuple[Path, list[dict[str, Any]]]:
        timeline = self._extract_timeline(edit_response)
        if not timeline:
            raise RuntimeError("raw_edit final_video_plan.timeline is empty")

        rendered_blocks: list[Path] = []
        rendered_timeline: list[dict[str, Any]] = []
        for index, item in enumerate(timeline, start=1):
            start = float(item.get("source_start", 0.0) or 0.0)
            end = float(item.get("source_end", start) or start)
            if end <= start:
                continue

            block_path = self.render_dir / f"raw_block_{index:03d}.mp4"
            self._render_block(
                source_video=source_video,
                output_path=block_path,
                start=start,
                end=end,
                role=str(item.get("role") or "development"),
            )
            rendered_blocks.append(block_path)
            rendered_timeline.append(
                {
                    **item,
                    "rendered_file": str(block_path),
                    "rendered_duration_sec": round(end - start, 3),
                }
            )

        if not rendered_blocks:
            raise RuntimeError("raw_edit timeline has no valid renderable blocks")

        output = output_path or (self.work_dir / "raw_edit_final.mp4")
        self._concat(rendered_blocks, output)
        return output, rendered_timeline

    def _extract_timeline(self, edit_response: dict[str, Any]) -> list[dict[str, Any]]:
        final_video_plan = edit_response.get("final_video_plan")
        if not isinstance(final_video_plan, dict):
            return []
        timeline = final_video_plan.get("timeline")
        if not isinstance(timeline, list):
            return []
        valid_items = [item for item in timeline if isinstance(item, dict)]
        return sorted(valid_items, key=lambda item: int(item.get("order") or 9999))

    def _render_block(
        self,
        *,
        source_video: Path,
        output_path: Path,
        start: float,
        end: float,
        role: str,
    ) -> None:
        duration = max(0.1, end - start)
        vf = self._video_filter(role)
        command = [
            "ffmpeg",
            "-y",
            "-ss",
            str(start),
            "-t",
            str(duration),
            "-i",
            str(source_video),
            "-vf",
            vf,
            "-af",
            "aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "22",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            "-avoid_negative_ts",
            "make_zero",
            str(output_path),
        ]
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _video_filter(self, role: str) -> str:
        base = [
            "scale=1920:1080:force_original_aspect_ratio=decrease",
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
            "fps=30",
            "format=yuv420p",
            "setsar=1",
            "setpts=PTS-STARTPTS",
            "eq=contrast=1.02:saturation=1.04:brightness=0.002",
            "vignette=angle=PI/30",
        ]
        if role in {"hook", "payoff", "cta"}:
            base.append("unsharp=3:3:0.22:3:3:0.0")
        return ",".join(base)

    def _concat(self, files: list[Path], output_path: Path) -> None:
        concat_list = self.render_dir / "raw_concat.txt"
        concat_list.write_text(
            "".join(f"file '{path.name}'\n" for path in files),
            encoding="utf-8",
        )
        command = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        subprocess.run(command, check=True, cwd=str(self.render_dir), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
