import json
import logging
import uuid
import os
import tempfile
import asyncio
import re

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from .queue_publisher import QueuePublisher
from .settings import settings
from .job_registry import JobRegistry
from .health_server import ControlPlaneHealth


logger = logging.getLogger(__name__)

publisher = QueuePublisher()
registry = JobRegistry()


class VoxmindBot:

    def __init__(self):

        self.app = ApplicationBuilder().token(settings.telegram_bot_token).build()

        self.app.add_handler(CommandHandler("new", self.handle_new))
        self.app.add_handler(CommandHandler("finalize", self.handle_finalize))

        self.app.add_handler(
            MessageHandler(filters.Document.FileExtension("json"), self.handle_document)
        )

        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text)
        )

    async def handle_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE):

        if not context.args:
            await update.message.reply_text(
                """
Uso:

/new [--short | --long | --short-serie]
     [--portrait | --landscape]
     [--build-ia]
     <url>
"""
            )
            return

        clip_mode = "short_serie"
        video_ratio = "portrait"
        build_ia = False
        video_url = None

        for arg in context.args:

            if arg == "--short":
                clip_mode = "short"

            elif arg == "--long":
                clip_mode = "long"

            elif arg == "--short-serie":
                clip_mode = "short_serie"

            elif arg == "--portrait":
                video_ratio = "portrait"

            elif arg == "--landscape":
                video_ratio = "landscape"

            elif arg == "--build-ia":
                build_ia = True

            elif arg.startswith("http"):
                video_url = arg

        if not video_url:

            await update.message.reply_text(
                "URL do vídeo não encontrada.\n\nUso: /new [flags] <url>"
            )

            return

        job_id = str(uuid.uuid4())

        registry.register(job_id, video_url)

        publisher.publish(
            video_url=video_url,
            job_id=job_id,
            pipeline_stage="prepare",
            clip_mode=clip_mode,
            video_ratio=video_ratio,
            build_ia=build_ia,
        )

        await update.message.reply_text(
            f"""
🎬 Pipeline iniciado

JOB_ID: {job_id}

Mode: {clip_mode}
Ratio: {video_ratio}
Auto IA: {"ON" if build_ia else "OFF"}

Aguarde o processamento.
"""
        )

    async def handle_finalize(self, update: Update, context: ContextTypes.DEFAULT_TYPE):

        document = update.message.document

        if not document:

            await update.message.reply_text(
                """
Envie o comando junto com o arquivo JSON.

Exemplo:

/finalize
📎 response.json
"""
            )

            return

        await self._process_json_document(update, context, document)

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):

        document = update.message.document

        await self._process_json_document(update, context, document)

    def _validate_shorts(self, data: dict):
        final_videos = data.get("final_videos")
        if isinstance(final_videos, list) and final_videos:
            self._validate_final_videos(final_videos)
            return

        shorts = data.get("shorts_content")

        if not isinstance(shorts, list) or not shorts:
            raise RuntimeError("shorts_content must be a non-empty list")

        for index, cut in enumerate(shorts):

            if "start" not in cut or "end" not in cut:
                raise RuntimeError(f"Cut {index} missing start/end")

            start = float(cut["start"])
            end = float(cut["end"])

            if start >= end:
                raise RuntimeError(f"Cut {index} start >= end")

            duration = end - start

            if duration < settings.min_cut_duration_sec:
                raise RuntimeError(
                    f"Cut {index} duration too short ({duration:.2f}s). Minimum is {settings.min_cut_duration_sec}s"
                )

    def _validate_final_videos(self, final_videos: list[dict]) -> None:
        for video_index, video in enumerate(final_videos, start=1):
            cuts = video.get("shorts_content") or []
            if not isinstance(cuts, list) or not cuts:
                raise RuntimeError(f"Video {video_index} must contain at least one cut")

            total_duration = 0.0
            for cut_index, cut in enumerate(cuts, start=1):
                if "start" not in cut or "end" not in cut:
                    raise RuntimeError(f"Video {video_index} cut {cut_index} missing start/end")

                start = float(cut["start"])
                end = float(cut["end"])
                if start >= end:
                    raise RuntimeError(f"Video {video_index} cut {cut_index} start >= end")

                duration = end - start
                total_duration += duration
                if duration < settings.min_internal_cut_duration_sec:
                    raise RuntimeError(
                        f"Video {video_index} cut {cut_index} duration too short ({duration:.2f}s). "
                        f"Minimum internal cut is {settings.min_internal_cut_duration_sec}s"
                    )

            if total_duration < settings.min_cut_duration_sec:
                raise RuntimeError(
                    f"Video {video_index} total duration too short ({total_duration:.2f}s). "
                    f"Minimum final video is {settings.min_cut_duration_sec}s"
                )

    def _normalize_finalize_payload(self, data: dict) -> dict:
        normalized = dict(data or {})
        final_videos = normalized.get("final_videos")
        if not isinstance(final_videos, list) or not final_videos:
            return normalized

        flattened_cuts = []
        normalized_videos = []

        for index, video in enumerate(final_videos, start=1):
            if not isinstance(video, dict):
                continue

            cuts = video.get("shorts_content") or []
            if not isinstance(cuts, list) or not cuts:
                continue

            post = video.get("post") or {}
            video_index = int(video.get("video_index") or index)
            normalized_videos.append(
                {
                    **video,
                    "video_index": video_index,
                    "post": post,
                    "shorts_content": cuts,
                }
            )

            for cut in cuts:
                if not isinstance(cut, dict):
                    continue
                flattened_cuts.append(
                    {
                        **cut,
                        "_post": post,
                        "_video_index": video_index,
                    }
                )

        if flattened_cuts:
            normalized["shorts_content"] = flattened_cuts
        if normalized_videos:
            normalized["final_videos"] = normalized_videos
            normalized["post"] = normalized_videos[0].get("post", {})

        return normalized

    def _sanitize_json_text(self, text: str) -> str:
        replacements = {
            "“": '"',
            "”": '"',
            "„": '"',
            "‟": '"',
            "’": "'",
            "‘": "'",
            "´": "'",
            "\ufeff": "",
        }
        sanitized = text
        for source, target in replacements.items():
            sanitized = sanitized.replace(source, target)
        sanitized = sanitized.strip()
        if sanitized.startswith("```"):
            sanitized = re.sub(r"^```(?:json)?\s*", "", sanitized, flags=re.IGNORECASE)
            sanitized = re.sub(r"\s*```$", "", sanitized)

        # Tolerate common manual-LLM artifacts like trailing commas.
        sanitized = re.sub(r",(\s*[}\]])", r"\1", sanitized)
        return sanitized

    def _parse_json_payload(self, text: str) -> dict:
        sanitized = self._sanitize_json_text(text)

        try:
            return json.loads(sanitized)
        except json.JSONDecodeError:
            start = sanitized.find("{")
            end = sanitized.rfind("}")
            if start != -1 and end != -1 and end > start:
                extracted = sanitized[start : end + 1]
                extracted = re.sub(r",(\s*[}\]])", r"\1", extracted)
                return json.loads(extracted)
            raise

    def _resolve_video_url(self, data: dict, job_id: str) -> str | None:
        video_url = data.get("video_url")
        if isinstance(video_url, str) and video_url.strip():
            registry.register(job_id, video_url.strip())
            return video_url.strip()

        return registry.get_video_url(job_id)

    def _collect_finalize_warnings(self, data: dict) -> list[str]:
        warnings: list[str] = []
        shorts = list(data.get("shorts_content") or [])
        clip_mode = str(data.get("clip_mode") or "").strip()

        generic_title_markers = {
            "o jogo por trás",
            "quem realmente manda",
            "o objetivo final",
            "o tamanho do poder",
        }

        for index, cut in enumerate(shorts, start=1):
            title = str(cut.get("title") or "").strip().lower()
            hook = str(cut.get("hook") or "").strip()

            if title in generic_title_markers:
                warnings.append(f"cut_{index}: título genérico")

            if hook and len(hook) < 18:
                warnings.append(f"cut_{index}: hook muito curto")

        if clip_mode == "short_serie" and len(shorts) >= 2:
            ordered = sorted(shorts, key=lambda item: float(item.get("start", 0.0)))

            for previous, current in zip(ordered, ordered[1:]):
                previous_group = previous.get("merge_group")
                current_group = current.get("merge_group")
                if previous_group and current_group and previous_group != current_group:
                    continue

                gap = float(current.get("start", 0.0)) - float(previous.get("end", 0.0))
                if gap > settings.short_serie_max_gap_sec:
                    warnings.append(
                        f"gap grande em short_serie: {gap:.1f}s entre {previous.get('end')} e {current.get('start')}"
                    )

        return warnings

    async def _handle_delivery_package(self, update: Update, data: dict):
        job_id = data.get("job_id", "unknown")
        delivery_status = data.get("delivery_status", "unknown")
        clip_count = data.get("clip_count", 0)
        qa_decision = data.get("qa_decision", "unknown")

        await update.message.reply_text(
            f"""
📦 Delivery package recebido

JOB_ID: {job_id}
Delivery status: {delivery_status}
QA: {qa_decision}
Clips: {clip_count}

Esse arquivo é informativo e pode ser consumido pelo ClipFlow Studio.
"""
        )

    async def _handle_qa_report(self, update: Update, data: dict):
        summary = data.get("summary", {})
        await update.message.reply_text(
            f"""
🧪 QA report recebido

Decision: {data.get("decision", "unknown")}
Approved: {summary.get("approved_clips", 0)}
Needs review: {summary.get("needs_review_clips", 0)}
Blocked: {summary.get("blocked_clips", 0)}

Esse arquivo é informativo e não dispara finalização.
"""
        )

    async def _handle_finalize_payload(self, update: Update, data: dict):
        data = self._normalize_finalize_payload(data)
        job_id = data.get("job_id")

        if not job_id:
            await update.message.reply_text("JSON precisa conter job_id.")
            return

        if "shorts_content" not in data:
            await update.message.reply_text(
                "JSON inválido. Campo obrigatório: shorts_content ou final_videos"
            )
            return

        try:
            self._validate_shorts(data)
        except Exception as e:
            await update.message.reply_text(f"JSON inválido: {str(e)}")
            return

        video_url = self._resolve_video_url(data, job_id)

        if not video_url:
            await update.message.reply_text(
                "Job ID não encontrado e video_url não foi informado no JSON."
            )
            return

        warnings = self._collect_finalize_warnings(data)
        if warnings:
            await update.message.reply_text(
                "⚠️ Avisos editoriais detectados antes do finalize:\n- "
                + "\n- ".join(warnings[:8])
            )

        publisher.publish(
            video_url=video_url,
            job_id=job_id,
            pipeline_stage="finalize",
            manual_response=data,
        )

        await update.message.reply_text(
            f"""
🚀 Finalização iniciada

JOB_ID: {job_id}

Gerando cortes...
"""
        )

    async def _process_json_document(self, update, context, document):

        try:

            if not document.file_name.endswith(".json"):

                await update.message.reply_text("Envie um arquivo JSON.")

                return

            file = await context.bot.get_file(document.file_id)

            tmp_dir = tempfile.gettempdir()

            unique_name = f"{uuid.uuid4()}_{document.file_name}"

            file_path = os.path.join(tmp_dir, unique_name)

            await file.download_to_drive(file_path)

            with open(file_path) as f:
                text = f.read()

            data = self._parse_json_payload(text)

        except Exception:

            await update.message.reply_text("JSON inválido.")

            return

        if "delivery_status" in data and "clips" in data:
            await self._handle_delivery_package(update, data)
            return

        if "decision" in data and "summary" in data and "clips" in data:
            await self._handle_qa_report(update, data)
            return

        await self._handle_finalize_payload(update, data)

        try:
            os.remove(file_path)
        except Exception:
            pass

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):

        try:
            data = self._parse_json_payload(update.message.text.strip())
        except Exception:
            return

        if "delivery_status" in data and "clips" in data:
            await self._handle_delivery_package(update, data)
            return

        if "decision" in data and "summary" in data and "clips" in data:
            await self._handle_qa_report(update, data)
            return

        await self._handle_finalize_payload(update, data)

    def run(self, health: ControlPlaneHealth | None = None):

        logger.info("Starting Voxmind Telegram Bot")
        if health:
            health.mark_ready("polling")

        try:
            try:
                asyncio.get_event_loop()
            except RuntimeError:
                asyncio.set_event_loop(asyncio.new_event_loop())

            self.app.run_polling()
        finally:
            if health:
                health.mark_not_ready("stopped")
