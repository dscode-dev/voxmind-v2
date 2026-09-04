"""The shape of one evaluated publication, and the contract an export promises.

**One row is one external publication.** Not one run: a run can render four clips, upload all
four, and get four different audiences. Rolling them into a single row would average away the
only comparison the data exists to support — which cut of the same match did better — and no
later aggregation could recover it.

**Decision context and outcomes are kept apart, structurally.** Everything under
``decision_context`` was knowable *before* the video existed; everything under ``outcomes``
happened afterwards. The separation is not cosmetic. The obvious future use of this dataset is
to learn something from it, and a table that mixes the two invites a model trained on
``views_24h`` to predict ``views_24h``. Keeping them in named groups — and in prefixed columns
once flattened — means leakage has to be introduced on purpose rather than by a careless
``SELECT *``.

``publication_context`` is a third group precisely because it is neither: privacy and
initiator were fixed at upload time, after selection and before any outcome. They are
confounders to condition on, not features of the decision and not results of it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.evaluation.windows import (
    WINDOW_POLICY_VERSION,
    WindowObservation,
    windows_in_order,
)

# The semantic version of the dataset as a whole: window definitions, resolution policy,
# derived field definitions and eligibility rules. Not a build date — two builds a week apart
# under the same rules are the same dataset definition, and a rule change on the same day is
# not.
DATASET_SEMANTIC_VERSION = "performance-eval-v1"

# The flat column contract. Versioned separately because a column can be appended to the
# export without any change to what the numbers mean.
EXPORT_SCHEMA_VERSION = "export-v1"

# Windows that get a views-per-hour column. Only the early ones: the rate is a first
# normalization for exposure time, and it stops being informative once a video's growth has
# flattened — a 7-day average hourly rate mostly measures the first evening, diluted.
RATE_WINDOWS = ("1h", "6h", "24h")

# The window engagement ratios are computed at. One window, not five: the ratio is a
# descriptive convenience, and computing it everywhere would suggest a precision the
# underlying observation lags do not support.
RATIO_WINDOW = "24h"


@dataclass
class DecisionContext:
    """Facts available when the system decided to produce this video.

    Read from the snapshot admission froze onto the PipelineJob wherever possible, not from
    the candidate as it stands today. The frozen copy is what the decision actually saw.
    """

    topic_id: str | None = None
    topic_name: str | None = None
    # "policy" or "manual". Load-bearing: a manually selected candidate may carry a score
    # from an earlier ranking that had nothing to do with why a person chose it, so any
    # analysis of score-versus-outcome has to be able to exclude these.
    selection_method: str | None = None
    selection_run_id: str | None = None
    selection_score: float | None = None
    score_version: str | None = None
    selected_at: datetime | None = None
    relevance_score: float | None = None
    trend_score: float | None = None
    clip_mode: str | None = None
    video_ratio: str | None = None
    source_provider: str | None = None
    source_channel: str | None = None
    source_external_id: str | None = None
    source_duration_sec: int | None = None
    source_published_at: datetime | None = None
    # How old the source video was when this system chose it. Both operands are persisted, so
    # this is arithmetic on recorded facts rather than a reconstruction.
    candidate_age_at_selection_sec: int | None = None


@dataclass
class PublicationContext:
    """Facts fixed at upload time. Confounders, not features and not results."""

    publish_target_id: str | None = None
    target_name: str | None = None
    initiator: str | None = None
    # What was asked for, frozen on the attempt when it was created.
    requested_privacy: str | None = None
    # What YouTube said it accepted. Usually equal, and the difference matters when it is not.
    accepted_privacy: str | None = None
    published_at: datetime | None = None
    media_bytes: int | None = None


@dataclass
class EvaluationRow:
    """One external publication, evaluated."""

    publish_attempt_id: str
    pipeline_job_id: str | None
    video_candidate_id: str | None
    external_video_id: str | None

    decision_context: DecisionContext
    publication_context: PublicationContext
    observations: dict[str, WindowObservation]

    # --------------------------------------------------------------- outcomes

    def outcomes(self) -> dict[str, Any]:
        """Absolute counters per window, plus the derived rates. Never a composite score."""
        out: dict[str, Any] = {}
        for window in windows_in_order():
            observation = self.observations.get(window.name)
            if observation is None:
                continue
            out[f"views_{window.name}"] = observation.view_count
            out[f"likes_{window.name}"] = observation.like_count
            out[f"comments_{window.name}"] = observation.comment_count
            out[f"availability_{window.name}"] = observation.availability
        out.update(self.derived())
        return out

    def derived(self) -> dict[str, float | None]:
        """The only arithmetic in this PR, and all of it exact.

        Views per hour divides by the observation's **actual** age, not the nominal window.
        Calling 1,100 views observed at 29h "views_per_hour_24h = 45.8" would be wrong by the
        five hours the collector was late; dividing by the real 29h gives 37.9, which is what
        was actually measured.
        """
        values: dict[str, float | None] = {}

        for name in RATE_WINDOWS:
            observation = self.observations.get(name)
            values[f"views_per_hour_{name}"] = _per_hour(observation)

        ratio = self.observations.get(RATIO_WINDOW)
        values[f"likes_per_view_{RATIO_WINDOW}"] = _ratio(
            _counter(ratio, "like_count"), _counter(ratio, "view_count")
        )
        values[f"comments_per_view_{RATIO_WINDOW}"] = _ratio(
            _counter(ratio, "comment_count"), _counter(ratio, "view_count")
        )
        return values

    # ------------------------------------------------------------------ trace

    def trace(self) -> dict[str, Any]:
        """Which observation supports each number.

        Without this a row says ``views_24h = 1100`` and offers no way to ask *when* that was
        seen or whether the collector was five hours late. A figure whose provenance cannot be
        checked is a figure nobody should build on.
        """
        entries: dict[str, Any] = {}
        for window in windows_in_order():
            observation = self.observations.get(window.name)
            if observation is None:
                continue
            entries[window.name] = {
                "availability": observation.availability,
                "target_age_seconds": observation.target_age_seconds,
                "tolerance_seconds": observation.tolerance_seconds,
                "snapshot_id": observation.snapshot_id,
                "observed_at": _iso(observation.observed_at),
                "actual_age_seconds": observation.actual_age_seconds,
                "observation_lag_seconds": observation.observation_lag_seconds,
            }
        return entries

    # ------------------------------------------------------------ serialization

    def as_dict(self) -> dict[str, Any]:
        """Grouped, so the decision/outcome boundary survives serialization."""
        return {
            "publish_attempt_id": self.publish_attempt_id,
            "pipeline_job_id": self.pipeline_job_id,
            "video_candidate_id": self.video_candidate_id,
            "external_video_id": self.external_video_id,
            "decision_context": _asdict(self.decision_context),
            "publication_context": _asdict(self.publication_context),
            "performance_outcomes": self.outcomes(),
            "observation_trace": self.trace(),
        }

    def as_flat(self) -> dict[str, Any]:
        """One flat record for CSV, with the group encoded in the prefix.

        ``dc_`` / ``pub_`` / ``out_`` / ``trace_`` keep the boundary legible after flattening,
        so someone loading the CSV can drop every outcome column with one prefix filter rather
        than by remembering which names are results.
        """
        flat: dict[str, Any] = {
            "publish_attempt_id": self.publish_attempt_id,
            "pipeline_job_id": self.pipeline_job_id,
            "video_candidate_id": self.video_candidate_id,
            "external_video_id": self.external_video_id,
        }
        for key, value in _asdict(self.decision_context).items():
            flat[f"dc_{key}"] = value
        for key, value in _asdict(self.publication_context).items():
            flat[f"pub_{key}"] = value
        for key, value in self.outcomes().items():
            flat[f"out_{key}"] = value
        for window_name, entry in self.trace().items():
            flat[f"trace_{window_name}_snapshot_id"] = entry["snapshot_id"]
            flat[f"trace_{window_name}_observed_at"] = entry["observed_at"]
            flat[f"trace_{window_name}_actual_age_seconds"] = entry["actual_age_seconds"]
            flat[f"trace_{window_name}_lag_seconds"] = entry["observation_lag_seconds"]
        return flat


def export_columns() -> list[str]:
    """The CSV header, declared rather than discovered.

    Deriving it from the first row's keys would make the header depend on which publication
    happened to sort first, and would change silently when a field went missing. A stable
    column list is what makes two exports diffable.
    """
    columns = [
        "publish_attempt_id",
        "pipeline_job_id",
        "video_candidate_id",
        "external_video_id",
    ]
    columns += [f"dc_{name}" for name in _field_names(DecisionContext)]
    columns += [f"pub_{name}" for name in _field_names(PublicationContext)]

    for window in windows_in_order():
        columns += [
            f"out_views_{window.name}",
            f"out_likes_{window.name}",
            f"out_comments_{window.name}",
            f"out_availability_{window.name}",
        ]
    for name in RATE_WINDOWS:
        columns.append(f"out_views_per_hour_{name}")
    columns += [
        f"out_likes_per_view_{RATIO_WINDOW}",
        f"out_comments_per_view_{RATIO_WINDOW}",
    ]

    for window in windows_in_order():
        columns += [
            f"trace_{window.name}_snapshot_id",
            f"trace_{window.name}_observed_at",
            f"trace_{window.name}_actual_age_seconds",
            f"trace_{window.name}_lag_seconds",
        ]
    return columns


def schema_contract() -> dict[str, Any]:
    """What an export promises, carried alongside the export itself."""
    return {
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "dataset_semantic_version": DATASET_SEMANTIC_VERSION,
        "window_policy_version": WINDOW_POLICY_VERSION,
        "column_groups": {
            "identity": "publish_attempt_id, pipeline_job_id, video_candidate_id, external_video_id",
            "dc_": "decision context — known before publication",
            "pub_": "publication context — fixed at upload, neither feature nor outcome",
            "out_": "performance outcomes — known only after publication",
            "trace_": "which snapshot supports each outcome",
        },
        "null_representation": "empty field",
        "columns": export_columns(),
    }


# --------------------------------------------------------------------- helpers


def _per_hour(observation: WindowObservation | None) -> float | None:
    """Views per hour of real exposure, or NULL.

    NULL when the window was not measured, when the counter was not disclosed, or when the
    age is not positive. A rate over a zero-length denominator is not a small number, it is
    undefined.
    """
    if observation is None or not observation.measured:
        return None
    if observation.view_count is None or not observation.actual_age_seconds:
        return None
    hours = observation.actual_age_seconds / 3600
    if hours <= 0:
        return None
    return round(observation.view_count / hours, 4)


def _counter(observation: WindowObservation | None, attribute: str) -> int | None:
    if observation is None or not observation.measured:
        return None
    return getattr(observation, attribute)


def _ratio(numerator: int | None, denominator: int | None) -> float | None:
    """A rate, or NULL — never an invented zero.

    Two distinct cases both land on NULL, and both are deliberate. A hidden ``like_count`` is
    unknown, so any ratio built on it is unknown; turning it into 0.0 would report a video
    with hidden likes as having no engagement. And zero views means the denominator is
    undefined: "0 likes out of 0 views" is not 0% engagement, it is a question that has not
    been asked yet.
    """
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def _asdict(record: Any) -> dict[str, Any]:
    return {
        name: _plain(getattr(record, name)) for name in _field_names(type(record))
    }


def _field_names(record_type: type) -> list[str]:
    return [f.name for f in getattr(record_type, "__dataclass_fields__").values()]


def _plain(value: Any) -> Any:
    return _iso(value) if isinstance(value, datetime) else value


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
