import httpx
from tenacity import retry, stop_after_attempt, wait_exponential
from .settings import settings

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
async def notify(text: str) -> None:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(url, json={"chat_id": settings.telegram_chat_id, "text": text})
