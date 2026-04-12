from dataclasses import dataclass

from app.settings import settings


@dataclass(frozen=True)
class ClipPreset:
    preset_id: str
    clip_mode: str
    video_ratio: str
    render_intent: str
    max_final_videos: int
    min_final_duration_sec: float
    target_final_duration_sec: float
    max_final_duration_sec: float
    min_internal_cut_duration_sec: float
    chunk_min_duration_sec: float
    chunk_target_duration_sec: float
    chunk_max_duration_sec: float
    chunk_overlap_sec: float
    candidate_min_duration_sec: float
    candidate_preferred_duration_sec: float
    candidate_max_duration_sec: float
    candidate_max_per_window: int
    scorer_max_candidates: int
    scorer_max_per_window: int
    scorer_min_start_gap_sec: float
    scorer_overlap_iou_threshold: float
    scorer_prefer_thematic_continuity: bool
    scorer_thematic_similarity_threshold: float
    prompt_context_padding_sec: int
    prompt_min_total_segments: int
    prompt_max_segments_per_candidate: int
    sequence_bridge_max_gap_sec: float
    context_backfill_max_sec: float
    closure_extension_max_sec: float
    render_playback_speed: float
    visual_filter_profile: str
    transition_profile: str
    caption_style: str

    @property
    def is_long_form(self) -> bool:
        return self.clip_mode in {"long", "long_series"}

    @property
    def prompt_clip_mode(self) -> str:
        return "long" if self.is_long_form else self.clip_mode


def resolve_clip_preset(clip_mode: str | None, video_ratio: str | None) -> ClipPreset:
    normalized_mode = _normalize_clip_mode(clip_mode)
    normalized_ratio = _normalize_video_ratio(video_ratio)

    if normalized_mode in {"long", "long_series"} and not video_ratio:
        normalized_ratio = "landscape"

    preset_key = (normalized_mode, normalized_ratio)
    if preset_key == ("short", "portrait"):
        return _short_portrait()
    if preset_key == ("short_serie", "portrait"):
        return _short_series_portrait()
    if preset_key in {("long", "landscape"), ("long_series", "landscape")}:
        return _long_landscape(normalized_mode)
    if normalized_mode in {"long", "long_series"}:
        return _long_non_landscape(normalized_mode, normalized_ratio)
    if normalized_mode == "short":
        return _short_default(normalized_ratio)
    if normalized_mode == "short_serie":
        return _short_series_default(normalized_ratio)

    return _short_series_portrait()


def resolve_product_preset(job_preset: str | None) -> ClipPreset:
    value = str(job_preset or "").strip().lower().replace("-", "_")
    mapping = {
        "short_individual": ("short", "portrait"),
        "short_individual_portrait": ("short", "portrait"),
        "short_series": ("short_serie", "portrait"),
        "short_series_portrait": ("short_serie", "portrait"),
        "short_serie": ("short_serie", "portrait"),
        "long_single": ("long", "landscape"),
        "long_single_landscape": ("long", "landscape"),
        "long": ("long", "landscape"),
        "long_series": ("long_series", "landscape"),
        "long_series_landscape": ("long_series", "landscape"),
    }
    clip_mode, video_ratio = mapping.get(value, ("short_serie", "portrait"))
    return resolve_clip_preset(clip_mode, video_ratio)


def resolve_job_preset(
    job_preset: str | None,
    clip_mode: str | None,
    video_ratio: str | None,
) -> ClipPreset:
    if str(job_preset or "").strip():
        return resolve_product_preset(job_preset)
    return resolve_clip_preset(clip_mode, video_ratio)


def _normalize_clip_mode(clip_mode: str | None) -> str:
    value = str(clip_mode or "short_serie").strip().lower().replace("-", "_")
    aliases = {
        "serie": "short_serie",
        "short_series": "short_serie",
        "short_serie": "short_serie",
        "shorts": "short",
        "single": "short",
        "long_single": "long",
        "long_form": "long",
        "longform": "long",
        "long_series": "long_series",
        "long_serie": "long_series",
    }
    return aliases.get(value, value if value in {"short", "short_serie", "long", "long_series"} else "short_serie")


def _normalize_video_ratio(video_ratio: str | None) -> str:
    value = str(video_ratio or "portrait").strip().lower().replace("-", "_")
    aliases = {
        "9:16": "portrait",
        "vertical": "portrait",
        "reels": "portrait",
        "tiktok": "portrait",
        "16:9": "landscape",
        "horizontal": "landscape",
        "youtube": "landscape",
    }
    return aliases.get(value, value if value in {"portrait", "landscape"} else "portrait")


def _short_portrait() -> ClipPreset:
    return ClipPreset(
        preset_id="short_individual_portrait",
        clip_mode="short",
        video_ratio="portrait",
        render_intent="social_ready_short_form",
        max_final_videos=3,
        min_final_duration_sec=float(settings.render_min_final_video_duration_sec),
        target_final_duration_sec=75.0,
        max_final_duration_sec=float(settings.qa_max_clip_duration_sec),
        min_internal_cut_duration_sec=float(settings.render_min_internal_cut_duration_sec),
        chunk_min_duration_sec=24.0,
        chunk_target_duration_sec=40.0,
        chunk_max_duration_sec=60.0,
        chunk_overlap_sec=5.0,
        candidate_min_duration_sec=24.0,
        candidate_preferred_duration_sec=42.0,
        candidate_max_duration_sec=60.0,
        candidate_max_per_window=3,
        scorer_max_candidates=8,
        scorer_max_per_window=1,
        scorer_min_start_gap_sec=18.0,
        scorer_overlap_iou_threshold=0.55,
        scorer_prefer_thematic_continuity=False,
        scorer_thematic_similarity_threshold=0.12,
        prompt_context_padding_sec=32,
        prompt_min_total_segments=28,
        prompt_max_segments_per_candidate=18,
        sequence_bridge_max_gap_sec=float(settings.render_sequence_bridge_max_gap_sec),
        context_backfill_max_sec=float(settings.render_context_backfill_max_sec),
        closure_extension_max_sec=float(settings.render_final_closure_extension_max_sec),
        render_playback_speed=float(settings.render_playback_speed),
        visual_filter_profile="short_subtle_vignette",
        transition_profile="short_dynamic",
        caption_style="clean_subtitles",
    )


def _short_series_portrait() -> ClipPreset:
    return ClipPreset(
        preset_id="short_series_portrait",
        clip_mode="short_serie",
        video_ratio="portrait",
        render_intent="social_ready_short_series",
        max_final_videos=3,
        min_final_duration_sec=float(settings.render_min_final_video_duration_sec),
        target_final_duration_sec=75.0,
        max_final_duration_sec=float(settings.qa_max_clip_duration_sec),
        min_internal_cut_duration_sec=float(settings.render_min_internal_cut_duration_sec),
        chunk_min_duration_sec=26.0,
        chunk_target_duration_sec=46.0,
        chunk_max_duration_sec=68.0,
        chunk_overlap_sec=5.0,
        candidate_min_duration_sec=26.0,
        candidate_preferred_duration_sec=48.0,
        candidate_max_duration_sec=68.0,
        candidate_max_per_window=3,
        scorer_max_candidates=8,
        scorer_max_per_window=2,
        scorer_min_start_gap_sec=16.0,
        scorer_overlap_iou_threshold=0.55,
        scorer_prefer_thematic_continuity=True,
        scorer_thematic_similarity_threshold=0.16,
        prompt_context_padding_sec=32,
        prompt_min_total_segments=28,
        prompt_max_segments_per_candidate=18,
        sequence_bridge_max_gap_sec=float(settings.render_sequence_bridge_max_gap_sec),
        context_backfill_max_sec=float(settings.render_context_backfill_max_sec),
        closure_extension_max_sec=float(settings.render_final_closure_extension_max_sec),
        render_playback_speed=float(settings.render_playback_speed),
        visual_filter_profile="short_subtle_vignette",
        transition_profile="short_dynamic",
        caption_style="clean_subtitles",
    )


def _long_landscape(clip_mode: str) -> ClipPreset:
    return ClipPreset(
        preset_id="long_series_landscape" if clip_mode == "long_series" else "long_single_landscape",
        clip_mode=clip_mode,
        video_ratio="landscape",
        render_intent="editorial_long_form_excerpt",
        max_final_videos=2 if clip_mode == "long_series" else 1,
        min_final_duration_sec=float(settings.render_min_long_video_duration_sec),
        target_final_duration_sec=float(settings.render_target_long_video_duration_sec),
        max_final_duration_sec=float(settings.render_max_long_video_duration_sec),
        min_internal_cut_duration_sec=float(settings.render_min_long_internal_cut_duration_sec),
        chunk_min_duration_sec=90.0,
        chunk_target_duration_sec=180.0,
        chunk_max_duration_sec=260.0,
        chunk_overlap_sec=18.0,
        candidate_min_duration_sec=75.0,
        candidate_preferred_duration_sec=180.0,
        candidate_max_duration_sec=260.0,
        candidate_max_per_window=2,
        scorer_max_candidates=4,
        scorer_max_per_window=1,
        scorer_min_start_gap_sec=90.0,
        scorer_overlap_iou_threshold=0.45,
        scorer_prefer_thematic_continuity=True,
        scorer_thematic_similarity_threshold=0.08,
        prompt_context_padding_sec=72,
        prompt_min_total_segments=72,
        prompt_max_segments_per_candidate=max(36, int(settings.prompt_long_max_segments_per_candidate)),
        sequence_bridge_max_gap_sec=float(settings.render_long_max_inter_cut_gap_sec),
        context_backfill_max_sec=float(settings.render_long_context_backfill_max_sec),
        closure_extension_max_sec=float(settings.render_long_closure_extension_max_sec),
        render_playback_speed=1.0,
        visual_filter_profile="long_soft_vignette",
        transition_profile="long_editorial",
        caption_style="editorial_subtitles",
    )


def _long_non_landscape(clip_mode: str, video_ratio: str) -> ClipPreset:
    preset = _long_landscape(clip_mode)
    return ClipPreset(
        **{
            **preset.__dict__,
            "preset_id": "long_series_portrait" if clip_mode == "long_series" else "long_single_portrait",
            "video_ratio": video_ratio,
            "chunk_min_duration_sec": 75.0,
            "chunk_target_duration_sec": 160.0,
            "chunk_max_duration_sec": 240.0,
            "chunk_overlap_sec": 16.0,
        }
    )


def _short_default(video_ratio: str) -> ClipPreset:
    preset = _short_portrait()
    return ClipPreset(
        **{
            **preset.__dict__,
            "preset_id": f"short_individual_{video_ratio}",
            "video_ratio": video_ratio,
            "chunk_min_duration_sec": 26.0,
            "chunk_target_duration_sec": 48.0,
            "chunk_max_duration_sec": 72.0,
            "candidate_min_duration_sec": 26.0,
            "candidate_preferred_duration_sec": 50.0,
            "candidate_max_duration_sec": 72.0,
            "candidate_max_per_window": 4,
            "scorer_max_candidates": 10,
            "scorer_max_per_window": 2,
            "scorer_min_start_gap_sec": 12.0,
            "scorer_overlap_iou_threshold": 0.55,
            "scorer_prefer_thematic_continuity": False,
            "scorer_thematic_similarity_threshold": 0.14,
        }
    )


def _short_series_default(video_ratio: str) -> ClipPreset:
    preset = _short_series_portrait()
    return ClipPreset(
        **{
            **preset.__dict__,
            "preset_id": f"short_series_{video_ratio}",
            "video_ratio": video_ratio,
            "chunk_min_duration_sec": 28.0,
            "chunk_target_duration_sec": 50.0,
            "chunk_max_duration_sec": 75.0,
            "candidate_min_duration_sec": 28.0,
            "candidate_preferred_duration_sec": 52.0,
            "candidate_max_duration_sec": 75.0,
            "candidate_max_per_window": 4,
            "scorer_max_candidates": 10,
            "scorer_max_per_window": 2,
            "scorer_min_start_gap_sec": 12.0,
            "scorer_overlap_iou_threshold": 0.55,
            "scorer_prefer_thematic_continuity": False,
            "scorer_thematic_similarity_threshold": 0.14,
        }
    )
