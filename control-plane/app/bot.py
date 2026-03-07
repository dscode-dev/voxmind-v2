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
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text)
        )

    async def handle_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE):

        if not context.args:
            await update.message.reply_text("Uso: /new URL <url-do-video>")
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

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):

        text = update.message.text.strip()

        try:
            data = json.loads(text)
        except Exception:
            return

        required_keys = {"cuts", "title", "description", "hashtags"}

        if not required_keys.issubset(data.keys()):
            await update.message.reply_text("JSON inválido.")
            return

        job_id = data.get("job_id")

        if not job_id:
            await update.message.reply_text("JSON precisa conter job_id.")
            return

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

    def run(self):
        self.app.run_polling()