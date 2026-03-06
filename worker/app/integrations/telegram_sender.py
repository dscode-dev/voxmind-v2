import requests
from pathlib import Path
from app.settings import settings


class TelegramSender:

    def __init__(self):

        if not settings.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN not configured")

        if not settings.telegram_chat_id:
            raise RuntimeError("TELEGRAM_CHAT_ID not configured")

        self.base_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
        self.chat_id = settings.telegram_chat_id

    # =========================
    # Send text message
    # =========================
    def send_message(self, text: str):

        url = f"{self.base_url}/sendMessage"

        payload = {
            "chat_id": self.chat_id,
            "text": text
        }

        requests.post(url, json=payload, timeout=30)

    # =========================
    # Send document
    # =========================
    def send_document(self, file_path: str, caption: str | None = None):

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

            requests.post(
                url,
                data=data,
                files=files,
                timeout=120
            )

    # =========================
    # Send video
    # =========================
    def send_video(self, file_path: str, caption: str | None = None):

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

            requests.post(
                url,
                data=data,
                files=files,
                timeout=300
            )