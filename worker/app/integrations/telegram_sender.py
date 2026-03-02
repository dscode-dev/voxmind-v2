import requests
import os


class TelegramSender:

    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")

    def send_video(self, file_path: str, caption: str = ""):
        if not self.token or not self.chat_id:
            return

        url = f"https://api.telegram.org/bot{self.token}/sendVideo"

        with open(file_path, "rb") as video:
            requests.post(
                url,
                data={
                    "chat_id": self.chat_id,
                    "caption": caption
                },
                files={
                    "video": video
                }
            )