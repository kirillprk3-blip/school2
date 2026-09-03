import asyncio
import logging
import random
from typing import Optional

from vkbottle import DocMessagesUploader
from vkbottle.bot import Bot
from vkbottle.exception_factory import VKAPIError

logger = logging.getLogger(__name__)

# Официальный лимит VK на длину текста сообщения/подписи к вложению.
VK_TEXT_LIMIT = 4096

_RATE_LIMIT_CODE = 6  # "Too many requests per second"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    suffix = "…"
    return text[: max(limit - len(suffix), 0)] + suffix


async def _call_vk(coro_factory):
    """Выполняет VK API-запрос с одним повтором при rate-limit (error code 6)."""
    try:
        return await coro_factory()
    except VKAPIError[_RATE_LIMIT_CODE]:
        logger.warning("VK rate limit (error 6): жду 1с и повторяю запрос...")
        await asyncio.sleep(1)
        return await coro_factory()


class VKSender:
    """Обёртка над vkbottle Bot API для отправки в конкретную VK-беседу (vk_peer_id)."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self._doc_uploader = DocMessagesUploader(bot.api)

    async def _send_resilient(self, base_kwargs: dict, reply_to: Optional[int]) -> Optional[int]:
        """Отправляет messages.send с ретраем при rate-limit и одноразовым фолбэком
        без reply_to, если исходное (устаревшее/удалённое) сообщение уже не найдено."""
        try:
            return await _call_vk(lambda: self.bot.api.messages.send(
                **base_kwargs, reply_to=reply_to, random_id=random.randint(1, 2_147_483_647),
            ))
        except Exception as e:
            if reply_to is not None:
                logger.warning(
                    "VK send с reply_to=%s не удался (%s), повторяю без reply_to", reply_to, e,
                )
                try:
                    return await _call_vk(lambda: self.bot.api.messages.send(
                        **base_kwargs, random_id=random.randint(1, 2_147_483_647),
                    ))
                except Exception as e2:
                    logger.error("VK send не удался и без reply_to: %s", e2)
                    return None
            logger.error("VK send не удался: %s", e)
            return None

    async def send_text(self, peer_id: int, text: str, reply_to: Optional[int] = None) -> Optional[int]:
        text = _truncate(text, VK_TEXT_LIMIT)
        return await self._send_resilient({"peer_id": peer_id, "message": text}, reply_to)

    async def send_attachment(
        self, peer_id: int, attachment: str, caption: str = "", reply_to: Optional[int] = None,
    ) -> Optional[int]:
        """Отправляет уже загруженное на сервера VK вложение (см. router.py)."""
        caption = _truncate(caption, VK_TEXT_LIMIT)
        return await self._send_resilient(
            {"peer_id": peer_id, "message": caption, "attachment": attachment}, reply_to,
        )

    async def send_file(self, peer_id: int, file_path: str, caption: str = "") -> Optional[int]:
        try:
            attachment = await self._doc_uploader.upload(file_path, peer_id=peer_id)
        except Exception as e:
            logger.error("Не удалось загрузить файл в VK peer_id=%s: %s", peer_id, e)
            return None
        return await self.send_attachment(peer_id, attachment, caption)

    async def edit_text(self, peer_id: int, message_id: int, text: str) -> bool:
        text = _truncate(text, VK_TEXT_LIMIT)
        try:
            return bool(await _call_vk(lambda: self.bot.api.messages.edit(
                peer_id=peer_id, message_id=message_id, message=text,
            )))
        except Exception as e:
            logger.error("Не удалось отредактировать сообщение VK peer_id=%s, message_id=%s: %s", peer_id, message_id, e)
            return False

    async def delete_message(self, peer_id: int, message_id: int) -> bool:
        try:
            result = await _call_vk(lambda: self.bot.api.messages.delete(
                peer_id=peer_id, message_ids=[message_id], delete_for_all=True,
            ))
            return bool(result)
        except Exception as e:
            logger.error("Не удалось удалить сообщение VK peer_id=%s, message_id=%s: %s", peer_id, message_id, e)
            return False
