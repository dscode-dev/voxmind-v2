from __future__ import annotations

import logging
from dataclasses import dataclass

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BotDeps:
    orchestrator_v2: object
    allowed_chat_id: str | None


def _is_allowed(update: Update, allowed_chat_id: str | None) -> bool:
    if not allowed_chat_id:
        return True
    try:
        return str(update.effective_chat.id) == str(allowed_chat_id)
    except Exception:
        return False


def build_app(*, token: str, deps: BotDeps) -> Application:
    app = Application.builder().token(token).build()

    async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_allowed(update, deps.allowed_chat_id):
            return

        text = update.message.text if update.message else ""
        parts = text.split()

        # /new <url> [--v2]
        if len(parts) < 2:
            await update.message.reply_text("Uso: /new <url> [--v2]")
            return

        url = parts[1]
        is_v2 = any(p.strip() in ("--v2", "—v2", "–v2") for p in parts[2:])  # tolerate different dash chars

        if not is_v2:
            await update.message.reply_text("V1 não está implementada neste repo. Use: /new <url> --v2")
            return

        await update.message.reply_text("V2: processando...")

        try:
            result = deps.orchestrator_v2.run(url=url)
            cuts = result.get("scored", {}).get("cuts", [])
            title = result.get("copy", {}).get("title", "")
            await update.message.reply_text(
                f"✅ V2 finalizou.\nCortes: {len(cuts)}\nTítulo: {title}\n\n(JSON completo nos logs por enquanto.)"
            )
        except Exception as e:
            log.exception("telegram.v2_failed")
            await update.message.reply_text(f"❌ Falhou na V2: {e}")

    app.add_handler(CommandHandler("new", new_cmd))
    return app
