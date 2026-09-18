import requests
from pathlib import Path
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.observability.logging import get_logger
from app.settings import settings


class _Transient(requests.RequestException):
    """Vale tentar de novo: a rede caiu, ou o Telegram pediu para esperar."""


class TelegramSender:

    # Um destino errado é errado para o processo inteiro. Descoberto uma vez, desligado uma
    # vez — em vez de redescoberto a cada mensagem, com o aviso repetido dezenas de vezes.
    _disabled_reason: str | None = None

    def __init__(self, chat_id: str | None = None):
        """`chat_id` overrides the deployment default for this run.

        A pipeline names the chat it reports to, and the payload carries it here. Without one
        the environment default still applies, which is what a studio job — a video that
        belongs to no pipeline — should use.
        """
        self.logger = get_logger(__name__)

        if settings.telegram_disable_notifications:
            self.base_url = None
            self.chat_id = None
            return

        if not settings.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN not configured")

        resolved = str(chat_id or "").strip() or settings.telegram_chat_id
        if not resolved:
            raise RuntimeError("TELEGRAM_CHAT_ID not configured")

        self.base_url = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
        self.chat_id = resolved

    @retry(
        retry=retry_if_exception_type(_Transient),
        stop=stop_after_attempt(settings.integration_retry_attempts),
        wait=wait_exponential(
            multiplier=1,
            min=settings.integration_retry_min_sec,
            max=settings.integration_retry_max_sec,
        ),
        reraise=True,
    )
    def _post(self, url: str, **kwargs):
        """Repete o que pode dar certo da segunda vez, e só isso.

        Antes repetia qualquer ``RequestException``, e um 400 "chat not found" é uma
        ``HTTPError`` — ou seja, uma configuração errada custava o backoff inteiro em cada
        mensagem. Num job com dezenas de avisos isso somava minutos de espera por um
        resultado que nunca ia mudar.
        """
        try:
            response = requests.post(url, **kwargs)
        except requests.RequestException as exc:
            raise _Transient(str(exc)) from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise _Transient(f"telegram respondeu {response.status_code}")
        response.raise_for_status()
        return response

    # =========================
    # Send text message
    # =========================
    def send_message(self, text: str):
        if not self.base_url or TelegramSender._disabled_reason:
            return

        url = f"{self.base_url}/sendMessage"

        payload = {
            "chat_id": self.chat_id,
            "text": text
        }

        try:
            self._post(url, json=payload, timeout=settings.telegram_timeout_sec)
        except requests.HTTPError as exc:
            self._latch_off(exc)
            raise

    def _latch_off(self, exc: requests.HTTPError) -> None:
        """Um 4xx do Telegram é configuração, não azar.

        Chat inexistente, bot sem permissão, token revogado: nada disso melhora tentando de
        novo, e o pipeline não deve pagar por isso a cada aviso. O primeiro caso desliga a
        entrega para o processo, com um aviso — e o job segue, porque a notificação nunca
        foi parte do resultado.
        """
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if status is None or status >= 500 or status == 429:
            return
        TelegramSender._disabled_reason = self._safe_error(exc)
        self.logger.warning(
            "Telegram recusou a entrega; notificações desligadas para este processo",
            extra={
                "step": "telegram_disabled",
                "status": "failed",
                "chat_id": self.chat_id,
                "error": TelegramSender._disabled_reason,
            },
        )

    # =========================
    # Send document
    # =========================
    def send_document(self, file_path: str, caption: str | None = None):
        if not self.base_url or TelegramSender._disabled_reason:
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
        if not self.base_url or TelegramSender._disabled_reason:
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

    def send_video_safe(self, file_path: str, caption: str | None = None) -> bool:
        if not self.base_url:
            return False

        path = Path(file_path)
        try:
            self.send_video(file_path, caption=caption)
            return True
        except requests.RequestException as exc:
            self.logger.warning(
                "Telegram sendVideo failed; trying sendDocument fallback",
                extra={
                    "step": "telegram_send_video",
                    "status": "failed",
                    "file_name": path.name,
                    "file_size_bytes": path.stat().st_size if path.exists() else None,
                    "error": self._safe_error(exc),
                },
            )

        try:
            self.send_document(file_path, caption=caption)
            return True
        except requests.RequestException as exc:
            self.logger.warning(
                "Telegram sendDocument fallback failed; continuing without Telegram video delivery",
                extra={
                    "step": "telegram_send_document_fallback",
                    "status": "failed",
                    "file_name": path.name,
                    "file_size_bytes": path.stat().st_size if path.exists() else None,
                    "error": self._safe_error(exc),
                },
            )
            try:
                self.send_message(
                    "⚠️ O vídeo final foi renderizado, mas o Telegram recusou o upload. "
                    "Use o ClipFlow Studio para baixar o arquivo final."
                )
            except Exception:
                pass
            return False

    # =========================
    # Best-effort variants
    # =========================
    # Telegram is a notification surface, not part of the job result. A rendered video must
    # not be reported as failed because a chat message could not be delivered.

    def send_message_safe(self, text: str) -> bool:
        try:
            self.send_message(text)
            return True
        except Exception as exc:
            self.logger.warning(
                "Telegram sendMessage failed; continuing (notification is best-effort)",
                extra={
                    "step": "telegram_send_message",
                    "status": "failed",
                    "error": self._safe_error(exc)
                    if isinstance(exc, requests.RequestException)
                    else str(exc)[:300],
                },
            )
            return False

    def send_document_safe(self, file_path: str, caption: str | None = None) -> bool:
        try:
            self.send_document(file_path, caption=caption)
            return True
        except Exception as exc:
            self.logger.warning(
                "Telegram sendDocument failed; continuing (notification is best-effort)",
                extra={
                    "step": "telegram_send_document",
                    "status": "failed",
                    "error": self._safe_error(exc)
                    if isinstance(exc, requests.RequestException)
                    else str(exc)[:300],
                },
            )
            return False

    def _safe_error(self, exc: requests.RequestException) -> str:
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                return f"{response.status_code}: {response.text[:300]}"
            except Exception:
                return str(response.status_code)
        return str(exc)[:300]
