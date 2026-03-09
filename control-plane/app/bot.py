import json
import logging
import uuid
import os
import tempfile

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

        # comandos
        self.app.add_handler(CommandHandler("new", self.handle_new))
        self.app.add_handler(CommandHandler("finalize", self.handle_finalize))

        # json enviado como arquivo
        self.app.add_handler(
            MessageHandler(filters.Document.FileExtension("json"), self.handle_document)
        )

        # json enviado como texto
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
            f"""
🎬 Pipeline iniciado

JOB_ID: {job_id}

Aguarde a transcrição e o prompt.
"""
        )

    # ==================================================
    # /finalize + arquivo JSON
    # ==================================================

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

    # ==================================================
    # JSON enviado como arquivo (sem comando)
    # ==================================================

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):

        document = update.message.document

        await self._process_json_document(update, context, document)

    # ==================================================
    # processamento central do JSON
    # ==================================================

    async def _process_json_document(self, update, context, document):

        try:

            if not document.file_name.endswith(".json"):

                await update.message.reply_text("Envie um arquivo JSON.")

                return

            file = await context.bot.get_file(document.file_id)

            tmp_dir = tempfile.gettempdir()

            file_path = os.path.join(tmp_dir, document.file_name)

            await file.download_to_drive(file_path)

            with open(file_path) as f:
                text = f.read()

            text = text.replace("“", '"').replace("”", '"')
            data = json.loads(text)

        except Exception:

            await update.message.reply_text("JSON inválido.")

            return

        job_id = data.get("job_id")

        if not job_id:

            await update.message.reply_text("JSON precisa conter job_id.")

            return

        if "shorts_content" not in data:

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
            manual_response=data,
        )

        await update.message.reply_text(
            f"""
🚀 Finalização iniciada

JOB_ID: {job_id}

Gerando cortes...
"""
        )

        try:
            os.remove(file_path)
        except Exception:
            pass

    # ==================================================
    # JSON enviado como texto
    # ==================================================

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):

        text = update.message.text.strip()

        try:

            data = json.loads(text)

        except Exception:

            return

        job_id = data.get("job_id")

        if not job_id:

            await update.message.reply_text("JSON precisa conter job_id.")

            return

        if "shorts_content" not in data:

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
            manual_response=data,
        )

        await update.message.reply_text(
            "🚀 Finalização iniciada! Gerando cortes..."
        )

    # ==================================================
    # run
    # ==================================================

    def run(self):

        logger.info("Starting Voxmind Telegram Bot")

        self.app.run_polling()