import os
from telegram import Bot
from minio import Minio


class TelegramSender:

    def __init__(self):
        self.bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"))
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")

        self.minio = Minio(
            "minio.voxmind-v2.svc.cluster.local:9000",
            access_key=os.getenv("MINIO_ROOT_USER"),
            secret_key=os.getenv("MINIO_ROOT_PASSWORD"),
            secure=False
        )

        self.bucket = os.getenv("MINIO_BUCKET", "voxmind-artifacts")

    def send_cuts(self, job_id: str):

        objects = self.minio.list_objects(
            self.bucket,
            prefix=f"{job_id}/cuts/",
            recursive=True
        )

        for obj in objects:

            response = self.minio.get_object(self.bucket, obj.object_name)

            self.bot.send_video(
                chat_id=self.chat_id,
                video=response
            )

            response.close()
            response.release_conn()