import requests
from pathlib import Path
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.observability.logging import get_logger
from app.settings import settings


class TelegramSender:

    def __init__(self):
        self.logger = get_logger(__name__)

        if settings.telegram_disable_notifications:
            self.base_url = None
            self.chat_id = None
            return

        if not settings.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN not configured")

        if not settings.telegram_chat_id:
            raise RuntimeError("TELEGRAM_CHAT_ID not configured")

        self.base_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
        self.chat_id = settings.telegram_chat_id

    @retry(
        retry=retry_if_exception_type(requests.RequestException),
        stop=stop_after_attempt(settings.integration_retry_attempts),
        wait=wait_exponential(
            multiplier=1,
            min=settings.integration_retry_min_sec,
            max=settings.integration_retry_max_sec,
        ),
        reraise=True,
    )
    def _post(self, url: str, **kwargs):
        response = requests.post(url, **kwargs)
        response.raise_for_status()
        return response

    # =========================
    # Send text message
    # =========================
    def send_message(self, text: str):
        if not self.base_url:
            return

        url = f"{self.base_url}/sendMessage"

        payload = {
            "chat_id": self.chat_id,
            "text": text
        }

        self._post(url, json=payload, timeout=settings.telegram_timeout_sec)

    # =========================
    # Send document
    # =========================
    def send_document(self, file_path: str, caption: str | None = None):
        if not self.base_url:
            return

        url = f"{self.base_url}/sendDocument"

        with open(file_path, "rb") as f:

            files = {
                "document": (Path(file_path).name, f)
            }

            data = {
                "chat_id": self.chat_id
            }

            if caption:
                data["caption"] = caption

            self._post(
                url,
                data=data,
                files=files,
                timeout=settings.telegram_upload_timeout_sec
            )

    # =========================
    # Send video
    # =========================
    def send_video(self, file_path: str, caption: str | None = None):
        if not self.base_url:
            return

        url = f"{self.base_url}/sendVideo"

        with open(file_path, "rb") as f:

            files = {
                "video": (Path(file_path).name, f)
            }

            data = {
                "chat_id": self.chat_id
            }

            if caption:
                data["caption"] = caption

            self._post(
                url,
                data=data,
                files=files,
                timeout=settings.telegram_upload_timeout_sec
            )
