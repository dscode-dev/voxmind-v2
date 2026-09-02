"""Offline evaluation runner.

Drives the **real** editorial code — the same Chunker, detectors, CandidateBuilder, Scorer,
SpanCatalogBuilder, prompt assembly, structural validation, normalization, cutter planning,
QA and auto-review the worker uses — with no Redis, MinIO, Telegram, network, download,
ffmpeg or real provider.

Only the IO boundaries are replaced:

  * MinioStorage / TelegramSender / ClipFlowApiClient → inert stubs (Pipeline constructs
    them eagerly; none is consulted on the editorial path);
  * AudioPeakDetector.analyze → identity (it shells out to ffmpeg for RMS energy);
  * the AI provider → a scripted responder defined by the case.

Everything that decides what a cut *is* runs for real, which is what makes the numbers
meaningful.
"""
from __future__ import annotations

import json
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest import mock

from evaluation import metrics as m


DATASETS_ROOT = Path(__file__).resolve().parent / "datasets"


@dataclass
class Case:
    case_id: str
    path: Path
    metadata: Dict[str, Any]
    transcript: List[Dict[str, Any]]
    expected: Dict[str, Any] = field(default_factory=dict)
    ai_response: Optional[Dict[str, Any]] = None

    @property
    def source_type(self) -> str:
        return str(self.metadata.get("source_type") or "unknown")

    @property
    def clip_mode(self) -> str:
        return str(self.metadata.get("clip_mode") or "short_serie")

    @property
    def video_ratio(self) -> str:
        return str(self.metadata.get("video_ratio") or "portrait")

    @property
    def job_preset(self) -> Optional[str]:
        return self.metadata.get("job_preset")

    @property
    def is_labeled(self) -> bool:
        return any(self.expected.get(key) for key in self.expected)

    @property
    def diarization_status(self) -> str:
        """available when the transcript carries real speaker labels; degraded otherwise."""
        speakers = {str(s.get("speaker") or "UNKNOWN") for s in self.transcript}
        return "available" if speakers - {"UNKNOWN"} else "degraded"


def load_case(case_dir: Path) -> Case:
    metadata = _read_json(case_dir / "metadata.json") or {}
    transcript = (
        _read_json(case_dir / "transcript_with_speakers.json")
        or _read_json(case_dir / "transcript.json")
        or []
    )
    return Case(
        case_id=metadata.get("case_id") or case_dir.name,
        path=case_dir,
        metadata=metadata,
        transcript=transcript,
        expected=_read_json(case_dir / "expected.json") or {},
        ai_response=_read_json(case_dir / "ai_response.json"),
    )


def discover_cases(dataset: str = "voxmind") -> List[Case]:
    root = DATASETS_ROOT / dataset
    if not root.exists():
        return []
    return [
        load_case(child)
        for child in sorted(root.iterdir())
        if child.is_dir() and (child / "metadata.json").exists()
    ]


class ScriptedProvider:
    """Deterministic stand-in for an AIProvider.

    Either replays the case's recorded ``ai_response.json``, or synthesises a selection from
    the top-ranked candidates so a case can be evaluated without a recorded response. The
    second call (repair) replays the same script, so repair accounting is observable.
    """

    name = "scripted"
    model = "scripted-v1"

    def __init__(self, response: Dict[str, Any] | None = None, responses: List[Any] | None = None):
        self.response = response
        self.responses = list(responses or [])
        self.calls: List[tuple[str, str]] = []

    def healthcheck(self) -> bool:
        return True

    def generate_json(self, system_prompt: str, user_prompt: str, schema=None):
        self.calls.append((system_prompt, user_prompt))
        if self.responses:
            return self.responses.pop(0)
        return self.response


def synthesize_selection(offered_spans, *, max_videos: int = 2, per_video: int = 4) -> Dict[str, Any]:
    """Select from the spans the request *offered*, sampled across the catalogue.

    This models the one behaviour that matters for grounding: a model picks from whatever
    the prompt advertises as selectable. Before the fix the prompt advertised every span of
    the whole video while showing only head/middle/tail excerpts, so spread-out picks land
    in regions that were never displayed — which is precisely `blind_span_references`.
    After the fix, offered == shown, so the same sampling can never produce one.

    It is deliberately not an editorial judgement; it is a probe of the grounding contract.
    """
    spans = [s for s in (offered_spans or []) if s.get("span_id")]
    if not spans:
        return {"final_videos": []}

    videos: List[Dict[str, Any]] = []
    # Sample evenly across the offered catalogue rather than taking a prefix.
    stride = max(1, len(spans) // (max_videos * per_video))

    for index in range(max_videos):
        chunk = spans[index * per_video * stride :: stride][:per_video]
        if len(chunk) < 2:
            break
        videos.append(
            {
                "video_index": index + 1,
                "span_ids": [s["span_id"] for s in chunk],
                "title": f"Selection {index + 1}",
                "hook": str(chunk[0].get("text") or "")[:120],
                "hook_start": float(chunk[0]["start"]),
                "hook_end": float(chunk[0]["end"]),
                "description": "Generated by the evaluation harness from grounded spans.",
                "hashtags": ["#futebol", "#voxmind", "#corte"],
                "shorts_content": [],
            }
        )

    return {"final_videos": videos}


@dataclass
class CaseResult:
    case_id: str
    source_type: str
    ok: bool
    detail: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


def run_case(case: Case, *, provider_factory: Callable[[Any], Any] | None = None) -> CaseResult:
    """Run one case through the real editorial path and collect metrics."""
    from app.pipeline.audio_peak_detector import AudioPeakDetector
    from app.pipeline.cut_contract import DurationContract, assign_cut_ids
    from app.pipeline.presets import resolve_job_preset
    from app.settings import settings

    with ExitStack() as stack:
        _patch_io(stack)
        # Audio peak scoring shells out to ffmpeg; the detector's contribution is a score
        # column, so identity keeps the rest of the chain honest without media.
        stack.enter_context(
            mock.patch.object(
                AudioPeakDetector, "analyze", lambda self, video_path, chunks: chunks
            )
        )

        from app.pipeline.pipeline import Pipeline

        settings.pipeline_stage = "prepare"
        preset = resolve_job_preset(case.job_preset, case.clip_mode, case.video_ratio)

        pipeline = Pipeline(
            video_url="https://evaluation.local/case",
            job_id=f"eval-{case.case_id}",
            clip_mode=preset.clip_mode,
            video_ratio=preset.video_ratio,
            job_preset=preset.preset_id,
            build_ia=True,
        )

        detail: Dict[str, Any] = {"diarization_status": case.diarization_status}
        transcript = case.transcript

        # --- analysis chain (real) ---
        chunks = pipeline.chunker.chunk(transcript)
        chunks = pipeline.hook_detector.analyze(chunks)
        chunks = pipeline.story_shift_detector.analyze(chunks)
        candidates = pipeline.builder.build(chunks)
        ranked = pipeline.scorer.score(candidates)
        span_catalog = pipeline.span_catalog_builder.build(transcript)
        hook_candidates = pipeline.span_catalog_builder.build_hook_candidates(span_catalog)

        detail["chunks"] = len(chunks)
        detail["candidates_built"] = len(candidates)
        detail["candidates_ranked"] = len(ranked)
        detail["spans_total"] = len(span_catalog)

        # --- AI context (real assembly) ---
        system_prompt, user_prompt, schema = pipeline.prompt_builder_v2.build(
            transcript=transcript,
            candidates=ranked,
            span_catalog=span_catalog,
            hook_candidates=hook_candidates,
            job_id=pipeline.job_id,
            clip_mode=preset.clip_mode,
            video_ratio=preset.video_ratio,
            job_preset=preset.preset_id,
        )
        context = getattr(pipeline.prompt_builder_v2.api_builder, "last_context", None)

        detail["context"] = dict(context.stats) if context is not None else {}
        detail["prompt_chars"] = len(system_prompt) + len(user_prompt)
        detail["candidates_in_prompt"] = _count_candidates_in_prompt(user_prompt, ranked)

        # Grounding is measured from the prompt the model actually received, not from an
        # internal attribute, so the BEFORE (no context object) and AFTER runs use exactly
        # the same definition: a span is "shown" iff its text appears in that request.
        shown_span_ids = spans_present_in_prompt(span_catalog, user_prompt)
        detail["spans_offered_total"] = len(span_catalog)
        detail["spans_shown_in_prompt"] = len(shown_span_ids)
        if context is not None:
            detail["context"]["selectable_span_count"] = len(context.spans)
        else:
            # Legacy builder: it advertised the whole catalogue regardless of what it showed.
            detail["context"] = {
                "transcript_chars": len(user_prompt),
                "selectable_span_count": len(span_catalog),
                "total_span_count": len(span_catalog),
                "candidate_count": 0,
                "coverage_ratio": None,
                "truncated": None,
            }
        selectable = shown_span_ids

        # --- AI response ---
        if provider_factory is not None:
            provider = provider_factory(context)
        elif case.ai_response is not None:
            provider = ScriptedProvider(response=case.ai_response)
        else:
            # What this version of the builder advertises as selectable.
            offered_spans = list(context.spans) if context is not None else list(span_catalog)
            provider = ScriptedProvider(response=synthesize_selection(offered_spans))
            detail["spans_offered_to_model"] = len(offered_spans)

        from app.ai import validation as ai_validation
        from app.ai.validation import AIResponseValidationError

        try:
            if hasattr(ai_validation, "generate_validated_cuts"):
                validated, ai_stats = ai_validation.generate_validated_cuts(
                    lambda sp, up: provider.generate_json(sp, up, schema),
                    system_prompt,
                    user_prompt,
                )
            else:
                # Legacy: one call, structural check only, no repair.
                raw = provider.generate_json(system_prompt, user_prompt, schema)
                validated = ai_validation.validate_cuts_response(raw)
                ai_stats = {"attempts": 1, "valid": True, "repair_attempted": False,
                            "repair_success": False, "errors": []}
            detail["ai"] = ai_stats
            detail["structurally_valid"] = True
        except AIResponseValidationError as exc:
            detail["ai"] = {"attempts": 2, "valid": False, "repair_attempted": True,
                            "repair_success": False, "errors": [str(exc)]}
            detail["structurally_valid"] = False
            return CaseResult(case.case_id, case.source_type, ok=True, detail=detail)

        detail["blind_span_references"] = referenced_but_unshown(validated, selectable)

        # --- normalization (real) ---
        pipeline.manual_response = validated
        expanded = pipeline._expand_response_from_span_ids(
            validated, span_catalog=span_catalog, hook_candidates=hook_candidates
        )
        expanded = pipeline._normalize_response_schema(expanded)
        pipeline.manual_response = pipeline._enforce_response_preset_contract(
            expanded, transcript
        )
        specs = pipeline._build_final_video_specs(transcript)
        detail["final_video_specs"] = len(specs)

        contract = DurationContract.from_preset(
            preset,
            min_renderable_cut_duration_sec=getattr(
                settings, "render_min_renderable_cut_duration_sec", 1.0
            ),
        )
        detail["duration_contract"] = {
            "min_renderable_cut_duration_sec": contract.min_renderable_cut_duration_sec,
            "min_internal_cut_duration_sec": contract.min_internal_cut_duration_sec,
            "min_final_video_duration_sec": contract.min_final_video_duration_sec,
            "max_final_video_duration_sec": contract.max_final_video_duration_sec,
        }

        # --- cutter planning (real predicate, no ffmpeg) ---
        video_reports: List[Dict[str, Any]] = []
        for spec in specs:
            cuts = assign_cut_ids(spec.get("cuts") or [], video_index=int(spec.get("video_index") or 1))
            renderable, ledger = plan_cuts(pipeline.cutter, cuts)

            report = {
                "video_index": spec.get("video_index"),
                "cuts": len(cuts),
                "renderable": len(renderable),
                "ledger": ledger.as_dict(),
                "duration": m.evaluate_duration_contract(cuts, contract),
                "candidate_coverage": m.evaluate_candidate_coverage(cuts, ranked),
                "boundaries": [m.evaluate_boundaries(c, transcript) for c in cuts],
                "speaker": [
                    m.evaluate_speaker_continuity(c, transcript, case.diarization_status)
                    for c in cuts
                ],
            }
            video_reports.append(report)

        detail["videos"] = video_reports

        # --- QA + auto-review (real) ---
        detail["qa"] = _run_qa(pipeline, specs, transcript, case.diarization_status)

        return CaseResult(case.case_id, case.source_type, ok=True, detail=detail)


def _timestamp(seconds) -> str:
    total = max(int(float(seconds or 0.0)), 0)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def spans_present_in_prompt(span_catalog, user_prompt: str) -> set:
    """Span ids whose own line appears in the request. The grounding ground truth.

    Matches the rendered `[MM:SS - MM:SS] SPEAKER: text` line rather than a text substring:
    the fixture transcripts reuse the same sentences across the video, so a substring probe
    would report a span as "shown" because an identical sentence appeared elsewhere. The
    timestamped line is unique to the span.
    """
    shown = set()
    for span in span_catalog or []:
        span_id = str(span.get("span_id") or "")
        text = str(span.get("text") or "").strip()
        if not span_id or not text:
            continue
        line = (
            f"[{_timestamp(span.get('start'))} - {_timestamp(span.get('end'))}] "
            f"{span.get('speaker', 'UNKNOWN')}: {text}"
        )
        if line in user_prompt:
            shown.add(span_id)
    return shown


def referenced_but_unshown(response: Dict[str, Any], shown_span_ids: set) -> List[str]:
    """Span ids the model selected that were never shown to it."""
    referenced: set = set()
    for video in response.get("final_videos") or []:
        for span_id in (video.get("span_ids") or []):
            referenced.add(str(span_id))
    for cut in response.get("shorts_content") or []:
        if isinstance(cut, dict) and cut.get("span_id"):
            referenced.add(str(cut["span_id"]))
    for video in response.get("final_videos") or []:
        for cut in (video.get("shorts_content") or []):
            if isinstance(cut, dict) and cut.get("span_id"):
                referenced.add(str(cut["span_id"]))
    return sorted(referenced - set(shown_span_ids))


def plan_cuts(cutter, cuts):
    """Cutter behaviour, in whichever version is installed.

    New cutter: `plan()` returns an explicit ledger. Legacy cutter: reproduce its rule —
    `duration < min_clip_duration_sec` was skipped with `continue`, leaving no trace, so
    those cuts are counted as SILENT drops.
    """
    from app.pipeline.cut_contract import CutLedger, assign_cut_ids, cut_duration

    if hasattr(cutter, "plan"):
        return cutter.plan(cuts)

    threshold = float(getattr(cutter, "min_clip_duration_sec", 0.0))
    ledger = CutLedger()
    renderable = []
    for cut in assign_cut_ids(cuts):
        ledger.accept(cut["cut_id"])
        if cut_duration(cut) < threshold:
            continue  # silently dropped: not rendered, not recorded
        renderable.append(cut)
        ledger.plan(cut["cut_id"])
    return renderable, ledger


def _run_qa(pipeline, specs, transcript, diarization_status) -> Dict[str, Any]:
    """QA/auto-review over the normalized specs, without rendering media."""
    if not specs:
        return {"decision": None, "automation": None, "clips": 0}

    spec = specs[0]
    cuts = spec.get("cuts") or []
    post = spec.get("post") or {}

    class _FakeRendered:
        """Stands in for a rendered file whose probed duration matches the request."""

        def __init__(self, name: str, duration: float):
            self.name = name
            self._duration = duration

        def exists(self) -> bool:
            return True

    rendered = [
        _FakeRendered(f"final_clip_{i:02d}.mp4", float(c.get("end", 0)) - float(c.get("start", 0)))
        for i, c in enumerate(cuts, start=1)
    ]

    with mock.patch.object(
        type(pipeline.clip_qa),
        "_probe_duration",
        lambda self, path: getattr(path, "_duration", 0.0),
    ):
        try:
            report = pipeline.clip_qa.evaluate(
                requested_cuts=cuts,
                rendered_files=rendered,
                transcript_segments=transcript,
                post_metadata=post,
                diarization_status=diarization_status,
            )
        except TypeError:
            # Legacy QA: no post_metadata/diarization_status parameters.
            report = pipeline.clip_qa.evaluate(
                requested_cuts=cuts,
                rendered_files=rendered,
                transcript_segments=transcript,
            )

    automation = pipeline.auto_review_policy.evaluate(qa_report=report, cuts=cuts)
    return {
        "decision": report.get("decision"),
        "summary": report.get("summary"),
        "automation_status": automation.get("status"),
        "readiness_score": automation.get("readiness_score"),
        "clips": len(cuts),
    }


def _patch_io(stack: ExitStack) -> None:
    """Replace the three eager IO clients Pipeline builds in its constructor."""
    stack.enter_context(mock.patch("app.pipeline.pipeline.MinioStorage", _InertClient))
    stack.enter_context(mock.patch("app.pipeline.pipeline.TelegramSender", _InertClient))
    stack.enter_context(mock.patch("app.pipeline.pipeline.ClipFlowApiClient", _InertClient))


class _InertClient:
    """Accepts any call and does nothing. No network, no filesystem."""

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, _name):
        def _noop(*args, **kwargs):
            return None

        return _noop


def _count_candidates_in_prompt(user_prompt: str, ranked: List[Dict]) -> int:
    return sum(
        1
        for candidate in ranked
        if str(candidate.get("candidate_id") or "") and str(candidate["candidate_id"]) in user_prompt
    )


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
