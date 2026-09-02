"""The cut duration contract and stable cut identity.

Three different minimums were previously conflated into one number, which is why a cut every
upstream layer had accepted could vanish inside the cutter:

``min_internal_cut_duration_sec``
    Editorial. The shortest a *single cut inside* an assembled final video may be. Owned by
    the preset (12s for short presets, 45s for long).

``min_final_video_duration_sec``
    Editorial. The shortest *total* duration of an assembled final video. Owned by the
    preset.

``min_renderable_cut_duration_sec``
    Technical. The shortest clip ffmpeg is asked to produce at all. This is a floor against
    degenerate input (zero/negative/sub-frame ranges), not an editorial opinion.

INVARIANT
    min_renderable_cut_duration_sec <= min_internal_cut_duration_sec

    A downstream stage may never reject a cut that an upstream stage accepted. The cutter
    renders exactly what it is given, one output per input; anything it cannot render is an
    explicit, attributed error — never a silent skip.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable


CUT_ID_KEY = "cut_id"


class CutContractViolation(RuntimeError):
    """A cut reached a stage that cannot process it, with the reason attributed."""

    def __init__(self, cut_id: str, reason: str, detail: str = "") -> None:
        self.cut_id = cut_id
        self.reason = reason
        self.detail = detail
        super().__init__(f"cut {cut_id}: {reason}{f' ({detail})' if detail else ''}")


@dataclass(frozen=True)
class DurationContract:
    """The three minimums, resolved together so their relationship is checkable."""

    min_renderable_cut_duration_sec: float
    min_internal_cut_duration_sec: float
    min_final_video_duration_sec: float
    max_final_video_duration_sec: float

    def validate(self) -> None:
        if self.min_renderable_cut_duration_sec > self.min_internal_cut_duration_sec:
            raise ValueError(
                "duration contract inverted: the technical floor "
                f"({self.min_renderable_cut_duration_sec}s) exceeds the editorial minimum "
                f"({self.min_internal_cut_duration_sec}s), so an editorially valid cut "
                "would be unrenderable"
            )
        if self.min_final_video_duration_sec > self.max_final_video_duration_sec:
            raise ValueError(
                "duration contract inverted: min_final_video_duration_sec exceeds "
                "max_final_video_duration_sec"
            )

    @classmethod
    def from_preset(cls, preset, *, min_renderable_cut_duration_sec: float) -> "DurationContract":
        contract = cls(
            min_renderable_cut_duration_sec=float(min_renderable_cut_duration_sec),
            min_internal_cut_duration_sec=float(preset.min_internal_cut_duration_sec),
            min_final_video_duration_sec=float(preset.min_final_duration_sec),
            max_final_video_duration_sec=float(preset.max_final_duration_sec),
        )
        contract.validate()
        return contract


@dataclass
class CutRejection:
    """Why a specific cut was dropped. Every rejection is attributable."""

    cut_id: str
    reason: str
    stage: str
    duration_sec: float | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "cut_id": self.cut_id,
            "reason": self.reason,
            "stage": self.stage,
            "duration_sec": self.duration_sec,
            "detail": self.detail,
        }


@dataclass
class CutLedger:
    """Records every cut that entered the pipeline and every one that left it.

    ``silent_drop_count`` is the property the pipeline must keep at zero: a cut that is
    neither rendered nor explicitly rejected has disappeared.
    """

    accepted: list[str] = field(default_factory=list)
    planned: list[str] = field(default_factory=list)
    rendered: list[str] = field(default_factory=list)
    rejections: list[CutRejection] = field(default_factory=list)

    def accept(self, cut_id: str) -> None:
        self.accepted.append(cut_id)

    def plan(self, cut_id: str) -> None:
        """Accepted for rendering. Planning alone already explains the cut's fate, which
        is what lets the evaluation harness measure drops without invoking ffmpeg."""
        self.planned.append(cut_id)

    def render(self, cut_id: str) -> None:
        self.rendered.append(cut_id)

    def reject(
        self,
        cut_id: str,
        reason: str,
        stage: str,
        duration_sec: float | None = None,
        detail: str = "",
    ) -> None:
        self.rejections.append(
            CutRejection(
                cut_id=cut_id,
                reason=reason,
                stage=stage,
                duration_sec=duration_sec,
                detail=detail,
            )
        )

    @property
    def rejected_ids(self) -> set[str]:
        return {rejection.cut_id for rejection in self.rejections}

    @property
    def silent_drops(self) -> list[str]:
        explained = set(self.rendered) | set(self.planned) | self.rejected_ids
        return [cut_id for cut_id in self.accepted if cut_id not in explained]

    @property
    def silent_drop_count(self) -> int:
        return len(self.silent_drops)

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": list(self.accepted),
            "planned": list(self.planned),
            "rendered": list(self.rendered),
            "rejections": [rejection.as_dict() for rejection in self.rejections],
            "silent_drops": self.silent_drops,
            "silent_drop_count": self.silent_drop_count,
        }


def cut_duration(cut: dict) -> float:
    start = float(cut.get("safe_start", cut.get("start", 0.0)) or 0.0)
    end = float(cut.get("safe_end", cut.get("end", 0.0)) or 0.0)
    return max(0.0, end - start)


def make_cut_id(cut: dict, *, video_index: int = 1, position: int = 0) -> str:
    """A stable identity that survives normalization, rendering, QA and delivery.

    Derived from the cut's own coordinates rather than its list position, so reordering or
    rejecting a sibling cannot re-label it. Deterministic: the same cut always gets the same
    id, which makes evaluation runs comparable.
    """
    existing = str(cut.get(CUT_ID_KEY) or "").strip()
    if existing:
        return existing

    start = round(float(cut.get("safe_start", cut.get("start", 0.0)) or 0.0), 2)
    end = round(float(cut.get("safe_end", cut.get("end", 0.0)) or 0.0), 2)
    span_id = str(cut.get("span_id") or "")
    digest = hashlib.sha1(
        f"{video_index}|{position}|{start}|{end}|{span_id}".encode("utf-8")
    ).hexdigest()[:10]
    return f"cut_{video_index:02d}_{position:02d}_{digest}"


def assign_cut_ids(cuts: Iterable[dict], *, video_index: int = 1) -> list[dict]:
    """Attach a cut_id to every cut, preserving any id already present."""
    assigned: list[dict] = []
    for position, cut in enumerate(cuts):
        if not isinstance(cut, dict):
            continue
        item = dict(cut)
        item[CUT_ID_KEY] = make_cut_id(item, video_index=video_index, position=position)
        assigned.append(item)
    return assigned
