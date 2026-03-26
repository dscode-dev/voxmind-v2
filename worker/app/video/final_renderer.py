import subprocess
from pathlib import Path
from typing import Dict, List


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

        clips_plan = {
            int(item.get("clip_index", 0)): item
            for item in render_plan.get("clips", [])
            if item.get("clip_index")
        }

        use_fade = any(
            str((transition_plans[index - 1] or {}).get("transition_after") or "")
            in {"fade", "whoosh", "punch_in"}
            for index in range(1, len(prepared_files))
        )

        if cold_open_inserted:
            self._concat_prepared_files(prepared_files, output_path)
            return

        if len(prepared_files) == 1 or not use_fade:
            self._concat_prepared_files(prepared_files, output_path)
            return

        command = ["ffmpeg", "-y"]
        for prepared_file in prepared_files:
            command.extend(["-i", str(prepared_file)])

        video_labels: List[str] = []
        audio_labels: List[str] = []
        for index in range(len(prepared_files)):
            video_labels.append(f"[{index}:v]")
            audio_labels.append(f"[{index}:a]")

        filter_parts: List[str] = []
        current_video = video_labels[0]
        current_audio = audio_labels[0]
        elapsed = prepared_durations[0]

        for index in range(1, len(prepared_files)):
            clip_plan = transition_plans[index - 1] or {}
            transition = str(clip_plan.get("transition_after") or "hard_cut")
            duration_ms = int(clip_plan.get("transition_duration_ms") or 0)
            fade_sec = max(0.12, duration_ms / 1000.0) if transition == "fade" and duration_ms > 0 else 0.0
            if transition in {"whoosh", "punch_in"} and duration_ms > 0:
                fade_sec = max(0.12, duration_ms / 1000.0)

            next_video = video_labels[index]
            next_audio = audio_labels[index]
            out_video = f"[v{index}]"
            out_audio = f"[a{index}]"

            if transition in {"fade", "whoosh", "punch_in"} and fade_sec > 0:
                offset = max(0.0, elapsed - fade_sec)
                filter_parts.append(
                    f"{current_video}{next_video}"
                    f"xfade=transition=fade:duration={fade_sec}:offset={offset}"
                    f"{out_video}"
                )
                filter_parts.append(
                    f"{current_audio}{next_audio}"
                    f"acrossfade=d={fade_sec}:curve1=tri:curve2=tri"
                    f"{out_audio}"
                )
                elapsed = elapsed + prepared_durations[index] - fade_sec
            else:
                filter_parts.append(f"{current_video}{next_video}concat=n=2:v=1:a=0{out_video}")
                filter_parts.append(f"{current_audio}{next_audio}concat=n=2:v=0:a=1{out_audio}")
                elapsed = elapsed + prepared_durations[index]

            current_video = out_video
            current_audio = out_audio

        command.extend(
            [
                "-filter_complex",
                ";".join(filter_parts),
                "-map",
                current_video,
                "-map",
                current_audio,
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
        )

        subprocess.run(
            command,
            check=True,
            cwd=str(self.render_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

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
        if not bool(clip_plan.get("overlay_enabled", False)):
            command = [
                "ffmpeg",
                "-y",
                "-i",
                str(input_path),
                "-c",
                "copy",
                str(output_path),
            ]
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
                "-c",
                "copy",
                str(output_path),
            ]
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

        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-vf",
            drawtext,
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

        actual_duration = min(duration_sec, max(0.8, source_duration - relative_start_sec))
        teaser_path = self.render_dir / "prepared_00_hook.mp4"
        command = [
            "ffmpeg",
            "-y",
            "-ss",
            str(relative_start_sec),
            "-t",
            str(actual_duration),
            "-i",
            str(prepared_files[source_idx]),
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
            str(teaser_path),
        ]
        subprocess.run(
            command,
            check=True,
            cwd=str(self.render_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        intro_main_path = self.render_dir / "prepared_01_intro_main.mp4"
        intro_main_command = [
            "ffmpeg",
            "-y",
            "-i",
            str(prepared_files[0]),
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
            str(intro_main_path),
        ]
        subprocess.run(
            intro_main_command,
            check=True,
            cwd=str(self.render_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        prepared_files = [teaser_path, intro_main_path, *prepared_files[1:]]
        prepared_durations = [actual_duration, *prepared_durations]
        transition_plans = [{}, *transition_plans]

        return prepared_files, prepared_durations, transition_plans, True

    def _subtitle_filter(self, subtitle_path: Path) -> str:
        subtitle_file = str(subtitle_path).replace("\\", "\\\\").replace(":", "\\:")
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
            "copy",
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
