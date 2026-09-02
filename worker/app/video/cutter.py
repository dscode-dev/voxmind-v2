from pathlib import Path
from typing import Dict, List

from app.observability import get_logger
from app.pipeline.cut_contract import (
    CutContractViolation,
    CutLedger,
    assign_cut_ids,
    cut_duration,
)
from app.runtime.subprocess_runner import run_ffmpeg

logger = get_logger(__name__)


class VideoCutter:
    """Renders the cuts it is given, one output per input.

    This class used to hold an *editorial* minimum (25s) and `continue` past anything
    shorter, while the presets accepted internal cuts down to 12s. A cut every upstream
    stage had validated therefore vanished here with no log, no error and no record —
    taking the payoff of a two-cut short_serie with it, and shifting every downstream index
    (subtitles, QA, delivery) by one.

    The editorial minimum now lives upstream where it belongs. What remains here is a
    technical floor: a range ffmpeg cannot meaningfully encode is an explicit, attributed
    error, never a silent skip.
    """

    def __init__(
        self,
        work_dir: Path,
        min_renderable_duration_sec: float = 1.0,
        job_id: str | None = None,
    ):
        self.work_dir = work_dir
        self.min_renderable_duration_sec = float(min_renderable_duration_sec)
        self.job_id = job_id
        self.ledger = CutLedger()

    def plan(self, cuts: List[Dict]) -> tuple[List[Dict], CutLedger]:
        """Decide what will be rendered, without touching ffmpeg.

        Split out so the evaluation harness and the tests can assert the drop behaviour
        without needing a real video file.
        """
        ledger = CutLedger()
        renderable: List[Dict] = []

        for cut in assign_cut_ids(cuts):
            cut_id = cut["cut_id"]
            ledger.accept(cut_id)

            duration = cut_duration(cut)
            if duration <= 0:
                ledger.reject(
                    cut_id,
                    reason="non_positive_duration",
                    stage="cutter",
                    duration_sec=duration,
                )
                continue
            if duration < self.min_renderable_duration_sec:
                ledger.reject(
                    cut_id,
                    reason="below_technical_render_floor",
                    stage="cutter",
                    duration_sec=duration,
                    detail=f"floor={self.min_renderable_duration_sec}s",
                )
                continue

            ledger.plan(cut_id)
            renderable.append(cut)

        return renderable, ledger

    def cut(self, video_path: Path, cuts: List[Dict]) -> List[Path]:
        renderable, ledger = self.plan(cuts)
        self.ledger = ledger

        for rejection in ledger.rejections:
            # A rejection is always reported. It is never inferred from a shorter list.
            logger.warning(
                f"Cut not renderable: {rejection.reason}",
                extra={
                    "job_id": self.job_id,
                    "step": "cut_clip",
                    "status": "rejected",
                    "cut_id": rejection.cut_id,
                    "reason": rejection.reason,
                    "duration_sec": rejection.duration_sec,
                },
            )

        output_files: List[Path] = []

        for index, cut in enumerate(renderable, start=1):
            start = float(cut.get("safe_start", cut.get("start", 0.0)) or 0.0)
            end = float(cut.get("safe_end", cut.get("end", 0.0)) or 0.0)

            output_path = self.work_dir / f"cut_{index:02d}.mp4"

            command = [
                "ffmpeg",
                "-y",
                "-ss", str(start),
                "-to", str(end),
                "-i", str(video_path),
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "23",
                "-c:a", "aac",
                "-movflags", "+faststart",
                "-avoid_negative_ts", "make_zero",
                str(output_path),
            ]

            run_ffmpeg(command, step="cut_clip", job_id=self.job_id)

            ledger.render(cut["cut_id"])
            output_files.append(output_path)

        if ledger.silent_drop_count:
            # Unreachable by construction; kept as an assertion of the invariant.
            raise CutContractViolation(
                ledger.silent_drops[0],
                "silent_drop",
                f"{ledger.silent_drop_count} cut(s) neither rendered nor rejected",
            )

        return output_files
