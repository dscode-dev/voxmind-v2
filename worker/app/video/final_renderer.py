import subprocess
from pathlib import Path
from typing import Dict, List

from app.settings import settings


class FinalVideoRenderer:

    def __init__(self, work_dir: Path):
        self.work_dir = work_dir
        self.render_dir = self.work_dir / "rendered_sequence"
        self.render_dir.mkdir(parents=True, exist_ok=True)

    def render(
        self,
        *,
        cut_files: List[Path],
        render_plan: Dict,
        subtitle_path: Path | None = None,
    ) -> Path | None:
        if not cut_files:
            return None

        prepared_files: List[Path] = []
        prepared_durations: List[float] = []
        plan_by_index = {
            int(item.get("clip_index", 0)): item
            for item in render_plan.get("clips", [])
            if item.get("clip_index")
        }

        for index, cut_file in enumerate(cut_files, start=1):
            clip_plan = plan_by_index.get(index, {})
            output_path = self.render_dir / f"prepared_{index:02d}.mp4"
            self._render_clip_with_overlay(
                input_path=cut_file,
                output_path=output_path,
                clip_plan=clip_plan,
            )
            prepared_files.append(output_path)
            prepared_durations.append(self._probe_duration(output_path))

        final_output = self.work_dir / "final_reel.mp4"
        self._assemble_sequence(
            prepared_files=prepared_files,
            prepared_durations=prepared_durations,
            render_plan=render_plan,
            output_path=final_output,
        )

        soundtrack = render_plan.get("soundtrack") or {}
        if soundtrack.get("status") == "selected" and soundtrack.get("local_path"):
            mixed_output = self.work_dir / "final_reel_mixed.mp4"
            self._mix_soundtrack(
                input_path=final_output,
                soundtrack_path=Path(str(soundtrack["local_path"])),
                volume=float(soundtrack.get("mix_volume", 0.12) or 0.12),
                output_path=mixed_output,
            )
            mixed_output.replace(final_output)

        if subtitle_path is not None and subtitle_path.exists():
            burned_output = self.work_dir / "final_reel_burned.mp4"
            self._burn_subtitles(
                input_path=final_output,
                subtitle_path=subtitle_path,
                output_path=burned_output,
            )
            burned_output.replace(final_output)

        return final_output

    def render_clip(
        self,
        *,
        input_path: Path,
        clip_plan: Dict,
        subtitle_path: Path | None = None,
        soundtrack: Dict | None = None,
        output_path: Path,
    ) -> Path:
        prepared_path = self.render_dir / f"{output_path.stem}_prepared.mp4"
        self._render_clip_with_overlay(
            input_path=input_path,
            output_path=prepared_path,
            clip_plan=clip_plan,
        )

        current_output = prepared_path
        soundtrack = soundtrack or {}
        if soundtrack.get("status") == "selected" and soundtrack.get("local_path"):
            mixed_output = self.render_dir / f"{output_path.stem}_mixed.mp4"
            self._mix_soundtrack(
                input_path=current_output,
                soundtrack_path=Path(str(soundtrack["local_path"])),
                volume=float(soundtrack.get("mix_volume", 0.12) or 0.12),
                output_path=mixed_output,
            )
            current_output = mixed_output

        if subtitle_path is not None and subtitle_path.exists():
            burned_output = self.render_dir / f"{output_path.stem}_burned.mp4"
            self._burn_subtitles(
                input_path=current_output,
                subtitle_path=subtitle_path,
                output_path=burned_output,
            )
            current_output = burned_output

        if current_output != output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            current_output.replace(output_path)

        return output_path

    def _assemble_sequence(
        self,
        *,
        prepared_files: List[Path],
        prepared_durations: List[float],
        render_plan: Dict,
        output_path: Path,
    ) -> None:
        if not prepared_files:
            raise RuntimeError("No prepared clips to assemble")

        prepared_files, prepared_durations, transition_plans, cold_open_inserted = self._prepend_cold_open(
            prepared_files=prepared_files,
            prepared_durations=prepared_durations,
            render_plan=render_plan,
        )
        if len(prepared_files) == 1:
            self._concat_prepared_files(prepared_files, output_path)
            return

        transition_ready_files = self._prepare_transition_edges(
            prepared_files=prepared_files,
            prepared_durations=prepared_durations,
            transition_plans=transition_plans,
        )
        self._concat_prepared_files(transition_ready_files, output_path)

    def _prepare_transition_edges(
        self,
        *,
        prepared_files: List[Path],
        prepared_durations: List[float],
        transition_plans: List[Dict],
    ) -> List[Path]:
        output_files: List[Path] = []
        for index, input_path in enumerate(prepared_files):
            incoming_plan = transition_plans[index - 1] if index > 0 and index - 1 < len(transition_plans) else {}
            outgoing_plan = transition_plans[index] if index < len(transition_plans) else {}
            duration = prepared_durations[index] if index < len(prepared_durations) else self._probe_duration(input_path)
            transitioned_path = self.render_dir / f"{input_path.stem}_transitioned.mp4"
            self._render_transitioned_clip(
                input_path=input_path,
                output_path=transitioned_path,
                duration=duration,
                incoming_plan=incoming_plan or {},
                outgoing_plan=outgoing_plan or {},
            )
            output_files.append(transitioned_path)
        return output_files

    def _render_transitioned_clip(
        self,
        *,
        input_path: Path,
        output_path: Path,
        duration: float,
        incoming_plan: Dict,
        outgoing_plan: Dict,
    ) -> None:
        video_filters: List[str] = ["fps=30", "format=yuv420p", "setsar=1", "setpts=PTS-STARTPTS"]
        audio_filters: List[str] = ["aresample=async=1:first_pts=0", "asetpts=PTS-STARTPTS"]

        fade_in_sec = self._transition_fade_seconds(incoming_plan)
        fade_out_sec = self._transition_fade_seconds(outgoing_plan)
        usable_duration = max(0.0, duration)

        if fade_in_sec > 0.0 and usable_duration > fade_in_sec + 0.15:
            video_filters.append(f"fade=t=in:st=0:d={fade_in_sec}")
            audio_filters.append(f"afade=t=in:st=0:d={fade_in_sec}")

        if fade_out_sec > 0.0 and usable_duration > fade_out_sec + 0.15:
            fade_out_start = max(0.0, usable_duration - fade_out_sec)
            video_filters.append(f"fade=t=out:st={fade_out_start}:d={fade_out_sec}")
            audio_filters.append(f"afade=t=out:st={fade_out_start}:d={fade_out_sec}")

        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-vf",
            ",".join(video_filters),
            "-af",
            ",".join(audio_filters),
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        subprocess.run(
            command,
            check=True,
            cwd=str(self.render_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _transition_fade_seconds(self, transition_plan: Dict | None) -> float:
        transition = str((transition_plan or {}).get("transition_after") or "").strip().lower()
        duration_ms = int((transition_plan or {}).get("transition_duration_ms") or 0)
        if transition not in {"fade", "whoosh", "punch_in"} or duration_ms <= 0:
            return 0.0
        return max(0.12, duration_ms / 1000.0)

    def _concat_prepared_files(self, prepared_files: List[Path], output_path: Path) -> None:
        concat_list = self.render_dir / "concat.txt"
        concat_list.write_text(
            "".join(f"file '{path.name}'\n" for path in prepared_files),
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
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "22",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(output_path),
        ]

        subprocess.run(
            command,
            check=True,
            cwd=str(self.render_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _render_clip_with_overlay(
        self,
        *,
        input_path: Path,
        output_path: Path,
        clip_plan: Dict,
    ) -> None:
        video_filters: List[str] = ["fps=30"]
        if settings.render_visual_filter_enabled:
            video_filters.extend(
                [
                    "eq=contrast=1.04:brightness=0.02:saturation=1.08",
                    "unsharp=5:5:0.45:5:5:0.0",
                ]
            )
        if settings.render_playback_speed and abs(settings.render_playback_speed - 1.0) > 0.01:
            video_filters.append(f"setpts=PTS/{settings.render_playback_speed}")
        video_filters.append("format=yuv420p")
        audio_filters: List[str] = []
        if settings.render_playback_speed and abs(settings.render_playback_speed - 1.0) > 0.01:
            audio_filters.append(f"atempo={settings.render_playback_speed}")

        if not bool(clip_plan.get("overlay_enabled", False)):
            command = [
                "ffmpeg",
                "-y",
                "-i",
                str(input_path),
                "-vf",
                ",".join(video_filters),
            ]
            if audio_filters:
                command.extend(["-af", ",".join(audio_filters)])
            command.extend(
                [
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "22",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-ar",
                    "48000",
                    "-movflags",
                    "+faststart",
                    str(output_path),
                ]
            )
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return

        on_screen_text = str(clip_plan.get("on_screen_text") or "").strip()
        if not on_screen_text:
            command = [
                "ffmpeg",
                "-y",
                "-i",
                str(input_path),
                "-vf",
                ",".join(video_filters),
            ]
            if audio_filters:
                command.extend(["-af", ",".join(audio_filters)])
            command.extend(
                [
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "22",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-ar",
                    "48000",
                    "-movflags",
                    "+faststart",
                    str(output_path),
                ]
            )
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return

        caption_style = str(clip_plan.get("caption_style") or "clean_subtitles")
        emphasis_words = [
            str(word).strip()
            for word in clip_plan.get("emphasis_words", [])
            if str(word).strip()
        ]
        text_timing = clip_plan.get("text_timing") or {}
        text = self._apply_emphasis(on_screen_text, emphasis_words, caption_style)
        drawtext = self._build_drawtext(
            text=text,
            caption_style=caption_style,
            text_timing=text_timing,
        )
        video_filters.append(drawtext)

        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-vf",
            ",".join(video_filters),
        ]
        if audio_filters:
            command.extend(["-af", ",".join(audio_filters)])
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "22",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-movflags",
                "+faststart",
                str(output_path),
            ]
        )
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _build_drawtext(self, *, text: str, caption_style: str, text_timing: Dict) -> str:
        safe_text = self._escape_drawtext(text)
        style = self._caption_style_profile(caption_style)
        enable = self._drawtext_enable(text_timing)
        return (
            "drawtext="
            f"text='{safe_text}':"
            "fontcolor=white:"
            f"fontsize={style['fontsize']}:"
            "line_spacing=8:"
            "box=1:"
            f"boxcolor={style['boxcolor']}:"
            f"boxborderw={style['boxborderw']}:"
            "borderw=0:"
            f"x={style['x']}:"
            f"y={style['y']}:"
            f"enable='{enable}'"
        )

    def _caption_style_profile(self, caption_style: str) -> Dict[str, str | int]:
        if caption_style == "hook_overlay":
            return {
                "fontsize": 44,
                "boxcolor": "black@0.55",
                "boxborderw": 24,
                "x": "(w-text_w)/2",
                "y": "h*0.12",
            }

        if caption_style == "kinetic":
            return {
                "fontsize": 40,
                "boxcolor": "black@0.40",
                "boxborderw": 18,
                "x": "(w-text_w)/2",
                "y": "h*0.16",
            }

        return {
            "fontsize": 36,
            "boxcolor": "black@0.45",
            "boxborderw": 20,
            "x": "(w-text_w)/2",
            "y": "h*0.14",
        }

    def _apply_emphasis(self, text: str, emphasis_words: List[str], caption_style: str) -> str:
        if not emphasis_words:
            return text.upper() if caption_style == "hook_overlay" else text

        emphasized = text
        for word in emphasis_words:
            emphasized = emphasized.replace(word, word.upper())

        if caption_style == "hook_overlay":
            return emphasized.upper()
        return emphasized

    def _escape_drawtext(self, text: str) -> str:
        return (
            text.replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace("'", r"\'")
            .replace(",", r"\,")
            .replace("[", r"\[")
            .replace("]", r"\]")
            .replace("%", r"\%")
        )

    def _drawtext_enable(self, text_timing: Dict) -> str:
        entry = float(text_timing.get("entry_sec", 0.0) or 0.0)
        exit_time = float(text_timing.get("exit_sec", 0.0) or 0.0)
        if exit_time <= entry:
            return "gte(t,0)"
        return f"between(t,{entry},{exit_time})"

    def _probe_duration(self, video_path: Path) -> float:
        command = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
            return float(result.stdout.strip())
        except Exception:
            return 0.0

    def _burn_subtitles(
        self,
        *,
        input_path: Path,
        subtitle_path: Path,
        output_path: Path,
    ) -> None:
        subtitles_filter = self._subtitle_filter(subtitle_path)
        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-vf",
            subtitles_filter,
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        subprocess.run(
            command,
            check=True,
            cwd=str(self.render_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _prepend_cold_open(
        self,
        *,
        prepared_files: List[Path],
        prepared_durations: List[float],
        render_plan: Dict,
    ) -> tuple[List[Path], List[float], List[Dict], bool]:
        if not prepared_files:
            return prepared_files, prepared_durations, [], False

        clips_plan = {
            int(item.get("clip_index", 0)): item
            for item in render_plan.get("clips", [])
            if item.get("clip_index")
        }
        transition_plans = [
            clips_plan.get(index, {})
            for index in range(1, len(prepared_files) + 1)
        ]

        first_clip_plan = next(
            (item for item in render_plan.get("clips", []) if int(item.get("clip_index", 0)) == 1),
            None,
        )
        cold_open = (first_clip_plan or {}).get("cold_open") or {}
        if not cold_open.get("enabled"):
            return prepared_files, prepared_durations, transition_plans, False

        duration_sec = max(0.8, float(cold_open.get("duration_sec", 2.0) or 2.0))
        relative_start_sec = max(0.0, float(cold_open.get("relative_start_sec", 0.0) or 0.0))
        source_clip_index = int(cold_open.get("source_clip_index", 1) or 1)
        source_idx = max(0, source_clip_index - 1)
        if source_idx >= len(prepared_files):
            return prepared_files, prepared_durations, transition_plans, False

        source_duration = prepared_durations[source_idx] if prepared_durations else 0.0
        if source_duration <= 0.0 or relative_start_sec >= source_duration:
            return prepared_files, prepared_durations, transition_plans, False

        playback_speed = max(0.5, float(settings.render_playback_speed or 1.0))
        prepared_relative_start_sec = relative_start_sec / playback_speed
        prepared_duration_sec = duration_sec / playback_speed
        if prepared_relative_start_sec >= source_duration:
            return prepared_files, prepared_durations, transition_plans, False

        actual_duration = min(prepared_duration_sec, max(0.8, source_duration - prepared_relative_start_sec))
        teaser_path = self.render_dir / "prepared_00_hook.mp4"
        command = [
            "ffmpeg",
            "-y",
            "-ss",
            str(prepared_relative_start_sec),
            "-t",
            str(actual_duration),
            "-i",
            str(prepared_files[source_idx]),
            "-vf",
            "fps=30,format=yuv420p,setsar=1,setpts=PTS-STARTPTS",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-af",
            "aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            str(teaser_path),
        ]
        subprocess.run(
            command,
            check=True,
            cwd=str(self.render_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        if source_idx != 0:
            prepared_files = [teaser_path, *prepared_files]
            prepared_durations = [actual_duration, *prepared_durations]
            transition_plans = [
                {
                    "transition_after": str(cold_open.get("transition_after") or "fade"),
                    "transition_duration_ms": int(cold_open.get("transition_duration_ms") or 180),
                },
                *transition_plans,
            ]
            return prepared_files, prepared_durations, transition_plans, True

        intro_main_path = self.render_dir / "prepared_01_intro_main.mp4"
        intro_resume_sec = min(
            source_duration,
            prepared_relative_start_sec + actual_duration,
        )
        intro_main_command = [
            "ffmpeg",
            "-y",
            "-ss",
            str(intro_resume_sec),
            "-i",
            str(prepared_files[source_idx]),
            "-vf",
            "fps=30,format=yuv420p,setsar=1,setpts=PTS-STARTPTS",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-af",
            "aresample=async=1:first_pts=0,asetpts=PTS-STARTPTS",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            str(intro_main_path),
        ]
        subprocess.run(
            intro_main_command,
            check=True,
            cwd=str(self.render_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        resumed_duration = max(0.0, source_duration - intro_resume_sec)
        prepared_files = [teaser_path, intro_main_path, *prepared_files[1:]]
        prepared_durations = [actual_duration, resumed_duration, *prepared_durations[1:]]
        transition_plans = [
            {
                "transition_after": str(cold_open.get("transition_after") or "fade"),
                "transition_duration_ms": int(cold_open.get("transition_duration_ms") or 180),
            },
            *transition_plans,
        ]

        return prepared_files, prepared_durations, transition_plans, True

    def _subtitle_filter(self, subtitle_path: Path) -> str:
        try:
            subtitle_file = str(subtitle_path.resolve().relative_to(self.render_dir.resolve()))
        except ValueError:
            subtitle_file = str(subtitle_path.resolve())
        subtitle_file = (
            subtitle_file.replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace("'", r"\'")
            .replace(",", r"\,")
            .replace("[", r"\[")
            .replace("]", r"\]")
        )
        if subtitle_path.suffix.lower() == ".ass":
            return f"subtitles='{subtitle_file}'"
        style = (
            "FontName=Arial,"
            "FontSize=10,"
            "PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H80000000,"
            "BackColour=&H30000000,"
            "BorderStyle=1,"
            "Outline=1,"
            "Shadow=0,"
            "MarginV=24,"
            "Alignment=2"
        )
        return f"subtitles='{subtitle_file}':force_style='{style}'"

    def _mix_soundtrack(
        self,
        *,
        input_path: Path,
        soundtrack_path: Path,
        volume: float,
        output_path: Path,
    ) -> None:
        if not soundtrack_path.exists():
            return

        volume = max(0.03, min(volume, 0.22))
        command = [
            "ffmpeg",
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(soundtrack_path),
            "-i",
            str(input_path),
            "-filter_complex",
            (
                f"[0:a]volume={volume},afade=t=in:st=0:d=1.2,afade=t=out:st=28:d=1.6[bed];"
                "[1:a][bed]amix=inputs=2:duration=first:dropout_transition=2[aout]"
            ),
            "-map",
            "1:v",
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        subprocess.run(
            command,
            check=True,
            cwd=str(self.render_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
