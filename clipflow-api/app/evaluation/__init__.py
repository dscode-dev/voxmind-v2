"""Turning raw observations into comparable ones.

The layer above `app.metrics`: ingestion records what a video's counters were at whatever
moments the collector was awake, and this decides which of those moments answer the same
question for every publication, so two videos can be compared without the comparison secretly
being about their ages.

Nothing here talks to a provider and nothing here writes. Like the ingestion package, no
production module imports it -- discovery, selection, admission, production and publishing are
all unaware this exists, which is what keeps "evaluation, not optimization" a property of the
import graph rather than a promise in a docstring.
"""
from app.evaluation.schema import (
    DATASET_SEMANTIC_VERSION,
    EXPORT_SCHEMA_VERSION,
    DecisionContext,
    EvaluationRow,
    PublicationContext,
    export_columns,
    schema_contract,
)
from app.evaluation.windows import (
    AVAILABILITY_STATES,
    AVAILABLE,
    MISSING_SNAPSHOT,
    NOT_MATURE,
    VIDEO_NOT_RETURNED,
    WINDOW_NAMES,
    WINDOW_POLICY_VERSION,
    WINDOWS,
    Window,
    WindowObservation,
    resolve_all,
    resolve_window,
)

__all__ = [
    "AVAILABLE",
    "NOT_MATURE",
    "MISSING_SNAPSHOT",
    "VIDEO_NOT_RETURNED",
    "AVAILABILITY_STATES",
    "WINDOWS",
    "WINDOW_NAMES",
    "WINDOW_POLICY_VERSION",
    "Window",
    "WindowObservation",
    "resolve_window",
    "resolve_all",
    "DATASET_SEMANTIC_VERSION",
    "EXPORT_SCHEMA_VERSION",
    "DecisionContext",
    "PublicationContext",
    "EvaluationRow",
    "export_columns",
    "schema_contract",
]
