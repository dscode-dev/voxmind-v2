import json
import logging
import uuid

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


logger = logging.getLogger(__name__)

publisher = QueuePublisher()
registry = JobRegistry()


class VoxmindBot:

    def __init__(self):

        self.app = ApplicationBuilder().token(settings.telegram_bot_token).build()

        self.app.add_handler(CommandHandler("new", self.handle_new))
        self.app.add_handler(CommandHandler("finalize", self.finalize_command))

        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text)
        )

    # ==================================================
    # /new
    # ==================================================

    async def handle_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE):

        if not context.args:
            await update.message.reply_text("Uso: /new <url-do-video>")
            return

        video_url = context.args[-1]
        job_id = str(uuid.uuid4())

        registry.register(job_id, video_url)

        publisher.publish(
            video_url=video_url,
            job_id=job_id,
            pipeline_stage="prepare",
        )

        await update.message.reply_text(
            f"🎬 Pipeline iniciado!\nJob ID: {job_id}\nAguarde transcrição e prompt."
        )

    # ==================================================
    # /finalize
    # ==================================================

    async def finalize_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):

        message = update.message.text

        try:

            lines = message.split("\n", 1)

            header = lines[0]
            json_part = lines[1] if len(lines) > 1 else None

            parts = header.split()

            if len(parts) != 2:
                await update.message.reply_text(
                    "Formato inválido.\n\nUse:\n/finalize JOB_ID\n{json}"
                )
                return

            job_id = parts[1]

            if not json_part:
                await update.message.reply_text("JSON não encontrado.")
                return

            manual_response = json.loads(json_part)

            if "shorts_content" not in manual_response:
                await update.message.reply_text(
                    "JSON inválido. Campo obrigatório: shorts_content"
                )
                return

            video_url = registry.get_video_url(job_id)

            if not video_url:
                await update.message.reply_text("Job ID não encontrado.")
                return

            publisher.publish(
                video_url=video_url,
                job_id=job_id,
                pipeline_stage="finalize",
                manual_response=manual_response,
            )

            await update.message.reply_text(
                f"""
🚀 Pipeline FINALIZE enviado

JOB_ID: {job_id}

Gerando cortes...
"""
            )

        except json.JSONDecodeError:
            await update.message.reply_text("JSON inválido.")
        except Exception as e:
            logger.exception("Erro no finalize")
            await update.message.reply_text(
                f"Erro ao processar finalize:\n{str(e)}"
            )

    # ==================================================
    # fallback json message
    # ==================================================

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):

        text = update.message.text.strip()

        try:
            data = json.loads(text)
        except Exception:
            return

        if "job_id" not in data:
            await update.message.reply_text("JSON precisa conter job_id.")
            return

        if "shorts_content" not in data:
            await update.message.reply_text("JSON precisa conter shorts_content.")
            return

        job_id = data.get("job_id")

        video_url = registry.get_video_url(job_id)

        if not video_url:
            await update.message.reply_text("Job ID não encontrado.")
            return

        publisher.publish(
            video_url=video_url,
            job_id=job_id,
            pipeline_stage="finalize",
            manual_response=data,
        )

        await update.message.reply_text(
            "🚀 Finalização iniciada! Gerando cortes..."
        )

    # ==================================================
    # run
    # ==================================================

    def run(self):
        self.app.run_polling()