import asyncio
import logging

from .logging_setup import setup_logging
from .settings import settings
from .pipeline import run_pipeline
from .telegram_notify import send_message

setup_logging(settings.log_level)
log = logging.getLogger("voxmind.worker")

async def _notify(text: str) -> None:
    if settings.telegram_bot_token and settings.telegram_chat_id:
        await send_message(token=settings.telegram_bot_token, chat_id=settings.telegram_chat_id, text=text)

def main() -> None:
    log.info("worker.start", extra={"video_url": settings.video_url, "mode": settings.pipeline_mode})
    try:
        result = run_pipeline()
        log.info("worker.done", extra=result)
        asyncio.run(_notify(f"✅ VoxMind worker done. Segments: {result['segments']}"))
    except Exception as e:
        log.exception("worker.failed", extra={"error": str(e)})
        asyncio.run(_notify(f"❌ VoxMind worker failed: {e}"))
        raise

if __name__ == "__main__":
    main()
