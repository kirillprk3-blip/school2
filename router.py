import asyncio
import logging
from typing import Optional

import aiohttp
from aiogram import Bot as TgBot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BufferedInputFile, InputMediaPhoto, Message as TgMessage
from vkbottle import PhotoMessageUploader, VideoUploader, VoiceMessageUploader
from vkbottle.bot import Bot as VkBot, Message as VkMessage

from config import config
from database import (
    add_message_history,
    add_message_map,
    bump_user_activity,
    get_tg_message,
    get_user_nickname,
    get_user_settings,
    get_vk_message,
    get_vk_verification,
    is_conversation_member_cached,
    sanitize_display_name,
)
from vk_sender import VKSender

logger = logging.getLogger(__name__)

# Официальные лимиты Telegram Bot API: обычный текст и подпись к медиа
# лимитируются по-разному (caption заметно короче текста сообщения).
TG_TEXT_LIMIT = 4096
TG_CAPTION_LIMIT = 1024


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    suffix = "…"
    return text[: max(limit - len(suffix), 0)] + suffix


async def _call_tg(coro_factory):
    """Выполняет TG-запрос с одним повтором при FloodWait (TelegramRetryAfter)."""
    try:
        return await coro_factory()
    except TelegramRetryAfter as e:
        logger.warning("TG FloodWait: жду %sс и повторяю запрос...", e.retry_after)
        await asyncio.sleep(e.retry_after)
        return await coro_factory()


async def _download_bytes(url: str) -> Optional[bytes]:
    if not url:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                return await resp.read()
    except Exception as e:
        logger.error("Не удалось скачать файл по URL %s: %s", url, e)
        return None


def _best_size_url(sizes) -> str:
    """Выбирает URL наибольшего варианта из списка размеров (photo.sizes / sticker.images)."""
    sizes = sizes or []
    if not sizes:
        return ""
    best = max(sizes, key=lambda s: (s.width or 0) * (s.height or 0))
    return best.url or getattr(best, "src", None) or ""


async def _resolve_vk_name(vk_bot: VkBot, user_id: int) -> str:
    try:
        users = await vk_bot.api.users.get(user_ids=[user_id])
        if users:
            return f"{users[0].first_name} {users[0].last_name}".strip()
    except Exception as e:
        logger.warning("Не удалось получить имя VK-пользователя %s: %s", user_id, e)
    return str(user_id)


async def _display_name(
    platform: str, user_id: int, fallback: str, username: Optional[str] = None,
) -> str:
    """Имя для шапки пересланного сообщения.

    Приоритет: user_settings.custom_name -> (для TG) vk_verifications.vk_full_name
    (подтверждённый VK-профиль - надёжнее, чем то, что человек ввёл сам) ->
    users.nickname -> fallback (обычно from_user.full_name / имя из VK API).
    Для TG, если задан username, добавляется "(@username)" - это отдельно от
    имени, которое пользователь сам себе выбрал через /setname.
    """
    settings = await get_user_settings(str(user_id))
    name = settings["custom_name"] if settings and settings.get("custom_name") else None

    if name is None and platform == "tg":
        verification = await get_vk_verification(user_id)
        if verification and verification.get("vk_full_name"):
            name = verification["vk_full_name"]

    if name is None and platform == "tg":
        name = await get_user_nickname(user_id)

    if name is None:
        name = fallback

    if platform == "tg" and username:
        return f"{name} (@{username})"
    return name


def _vk_mention(vk_user_id: int, name: str) -> str:
    """Кликабельное VK-упоминание в формате VK-разметки. Работает только в
    сообщениях, отправляемых В VK - в Telegram такой синтаксис не рендерится,
    поэтому применяется только для направления TG->VK."""
    return f"[id{vk_user_id}|{name}]"


class MessageRouter:
    """Двусторонний маршрутизатор сообщений/медиа между привязанными VK и TG чатами."""

    def __init__(self, tg_bot: TgBot, vk_bot: VkBot):
        self.tg_bot = tg_bot
        self.vk_bot = vk_bot
        self.vk_sender = VKSender(vk_bot)

    # --- VK -> TG ---------------------------------------------------------

    async def handle_vk_message(self, bridge: dict, message: VkMessage) -> None:
        if message.from_id < 0:
            return  # сообщения от сообществ/групп трактуем как "ботов" (по ТЗ)

        tg_chat_id = bridge["tg_chat_id"]
        vk_peer_id = bridge["vk_peer_id"]

        fallback_name = await _resolve_vk_name(self.vk_bot, message.from_id)
        header = await _display_name("vk", message.from_id, fallback_name)

        reply_to_tg_msg_id: Optional[int] = None
        if message.reply_message:
            reply_row = await get_tg_message(vk_peer_id, message.reply_message.id)
            if reply_row:
                reply_to_tg_msg_id = reply_row["tg_msg_id"]

        attachments = message.attachments or []
        audio_messages = [a.audio_message for a in attachments if a.audio_message]
        photos = [a.photo for a in attachments if a.photo]
        videos = [a.video for a in attachments if a.video]
        stickers = [a.sticker for a in attachments if a.sticker]

        sent_tg_msg_id: Optional[int] = None
        try:
            if audio_messages:
                sent_tg_msg_id = await self._forward_vk_voice(tg_chat_id, header, audio_messages[0], reply_to_tg_msg_id)
            elif len(photos) > 1:
                sent_tg_msg_id = await self._forward_vk_photo_album(tg_chat_id, header, photos, reply_to_tg_msg_id)
            elif photos:
                sent_tg_msg_id = await self._forward_vk_photo(tg_chat_id, header, photos[0], message.text, reply_to_tg_msg_id)
            elif stickers:
                sent_tg_msg_id = await self._forward_vk_sticker(tg_chat_id, header, stickers[0], reply_to_tg_msg_id)
            elif videos:
                sent_tg_msg_id = await self._forward_vk_video(tg_chat_id, header, videos[0], message.text, reply_to_tg_msg_id)
            elif message.text:
                text = _truncate(f"{header}:\n{message.text}", TG_TEXT_LIMIT)
                sent = await _call_tg(lambda: self.tg_bot.send_message(
                    tg_chat_id, text, reply_to_message_id=reply_to_tg_msg_id,
                    allow_sending_without_reply=True,
                ))
                sent_tg_msg_id = sent.message_id
            elif attachments:
                # Вложение известного нам типа не подошло (документ, граффити, опрос,
                # геолокация и т.п.) - не теряем сообщение молча, шлём заглушку.
                text = _truncate(f"{header}: [вложение неподдерживаемого типа]", TG_TEXT_LIMIT)
                sent = await _call_tg(lambda: self.tg_bot.send_message(
                    tg_chat_id, text, reply_to_message_id=reply_to_tg_msg_id,
                    allow_sending_without_reply=True,
                ))
                sent_tg_msg_id = sent.message_id
        except Exception as e:
            logger.error("Не удалось переслать VK->TG сообщение (peer_id=%s): %s", vk_peer_id, e)
            return

        if sent_tg_msg_id is not None:
            await add_message_map(vk_peer_id, message.id, tg_chat_id, sent_tg_msg_id)
            await add_message_history(
                vk_peer_id, "vk_to_tg", sender_tg_user_id=None, sender_vk_user_id=message.from_id,
                display_name=header, text=message.text or None,
                has_media=bool(audio_messages or photos or videos or stickers),
                tg_msg_id=sent_tg_msg_id, vk_msg_id=message.id,
            )

    async def is_sender_in_conversation(self, vk_peer_id: int, vk_user_id: int) -> bool:
        """Проверка "участник ли этот VK-пользователь беседы vk_peer_id" - используется
        gate'ом пересылки TG->VK в tg_handlers.py перед forward, чтобы отправитель
        не мог форварднуть сообщение в чужую VK-беседу, к которой он не имеет отношения."""
        return await is_conversation_member_cached(self.vk_bot, vk_peer_id, vk_user_id)

    async def _forward_vk_voice(
        self, tg_chat_id: int, header: str, audio_message, reply_to: Optional[int] = None,
    ) -> Optional[int]:
        data = await _download_bytes(audio_message.link_ogg)
        if data is None:
            return None
        sent = await _call_tg(lambda: self.tg_bot.send_voice(
            tg_chat_id,
            BufferedInputFile(data, filename="voice.ogg"),
            caption=_truncate(header, TG_CAPTION_LIMIT),
            reply_to_message_id=reply_to,
            allow_sending_without_reply=True,
        ))
        return sent.message_id

    async def _forward_vk_photo(
        self, tg_chat_id: int, header: str, photo, caption_text: Optional[str], reply_to: Optional[int] = None,
    ) -> Optional[int]:
        data = await _download_bytes(_best_size_url(photo.sizes))
        if data is None:
            return None
        caption = _truncate(f"{header}:\n{caption_text}" if caption_text else header, TG_CAPTION_LIMIT)
        sent = await _call_tg(lambda: self.tg_bot.send_photo(
            tg_chat_id, BufferedInputFile(data, filename="photo.jpg"), caption=caption,
            reply_to_message_id=reply_to, allow_sending_without_reply=True,
        ))
        return sent.message_id

    async def _forward_vk_sticker(
        self, tg_chat_id: int, header: str, sticker, reply_to: Optional[int] = None,
    ) -> Optional[int]:
        data = await _download_bytes(_best_size_url(sticker.images))
        if data is None:
            return None
        sent = await _call_tg(lambda: self.tg_bot.send_photo(
            tg_chat_id, BufferedInputFile(data, filename="sticker.png"), caption=_truncate(header, TG_CAPTION_LIMIT),
            reply_to_message_id=reply_to, allow_sending_without_reply=True,
        ))
        return sent.message_id

    async def _forward_vk_photo_album(
        self, tg_chat_id: int, header: str, photos: list, reply_to: Optional[int] = None,
    ) -> Optional[int]:
        media = []
        for i, photo in enumerate(photos[:10]):
            data = await _download_bytes(_best_size_url(photo.sizes))
            if data is None:
                continue
            media.append(InputMediaPhoto(
                media=BufferedInputFile(data, filename=f"photo{i}.jpg"),
                caption=_truncate(header, TG_CAPTION_LIMIT) if i == 0 else None,
            ))
        if not media:
            return None
        sent = await _call_tg(lambda: self.tg_bot.send_media_group(
            tg_chat_id, media, reply_to_message_id=reply_to, allow_sending_without_reply=True,
        ))
        return sent[0].message_id if sent else None

    async def _forward_vk_video(
        self, tg_chat_id: int, header: str, video, caption_text: Optional[str], reply_to: Optional[int] = None,
    ) -> Optional[int]:
        # Прямого файла видео Bots LongPoll API без video.get с расширенными
        # правами не отдаёт - пересылаем ссылкой на плеер (см. STATE.md).
        player_url = video.player or ""
        text = _truncate(f"{header} (видео):\n{caption_text or ''}\n{player_url}".strip(), TG_TEXT_LIMIT)
        sent = await _call_tg(lambda: self.tg_bot.send_message(
            tg_chat_id, text, reply_to_message_id=reply_to, allow_sending_without_reply=True,
        ))
        return sent.message_id

    # --- TG -> VK ---------------------------------------------------------

    async def handle_tg_message(
        self, bridge: dict, message: TgMessage, text_override: Optional[str] = None,
    ) -> None:
        if message.from_user and message.from_user.is_bot:
            return

        vk_peer_id = bridge["vk_peer_id"]
        tg_chat_id = bridge["tg_chat_id"]

        fallback_name = message.from_user.full_name if message.from_user else "TG"
        user_id = message.from_user.id if message.from_user else 0
        username = message.from_user.username if message.from_user else None
        header = await _display_name("tg", user_id, fallback_name, username=username)

        # vk_header - то, что реально уходит В VK: если у отправителя есть
        # подтверждённая VK-страница, шапка становится кликабельным VK-упоминанием
        # [id<vk_user_id>|Имя] вместо простого текста. sanitize_display_name тут
        # обязателен - header может быть произвольным custom_name из /setname,
        # а не только санитизированным vk_full_name, и без очистки "[" "]" "|"
        # в имени можно было бы сломать/подделать разметку упоминания.
        vk_header = header
        verification = await get_vk_verification(user_id) if user_id else None
        if verification and verification.get("vk_user_id"):
            vk_header = _vk_mention(verification["vk_user_id"], sanitize_display_name(header))

        reply_to_vk_msg_id: Optional[int] = None
        if message.reply_to_message:
            reply_row = await get_vk_message(tg_chat_id, message.reply_to_message.message_id)
            if reply_row:
                reply_to_vk_msg_id = reply_row["vk_msg_id"]

        text_body = text_override if text_override is not None else message.text
        caption_body = text_override if text_override is not None else message.caption
        has_media = bool(message.voice or message.photo or message.video)

        sent_vk_msg_id: Optional[int] = None
        try:
            if message.voice:
                sent_vk_msg_id = await self._forward_tg_voice(
                    vk_peer_id, vk_header, message.voice.file_id, reply_to_vk_msg_id,
                )
            elif message.photo:
                sent_vk_msg_id = await self._forward_tg_photo(
                    vk_peer_id, vk_header, message.photo[-1].file_id, caption_body, reply_to_vk_msg_id,
                )
            elif message.video:
                sent_vk_msg_id = await self._forward_tg_video(
                    vk_peer_id, vk_header, message.video.file_id, caption_body, reply_to_vk_msg_id,
                )
            elif text_body:
                sent_vk_msg_id = await self.vk_sender.send_text(
                    vk_peer_id, f"{vk_header}:\n{text_body}", reply_to=reply_to_vk_msg_id,
                )
            else:
                # Стикер/документ/опрос/геолокация и т.п. - не поддерживаем конвертацию,
                # но и не теряем сообщение молча.
                sent_vk_msg_id = await self.vk_sender.send_text(
                    vk_peer_id, f"{vk_header}: [вложение неподдерживаемого типа]", reply_to=reply_to_vk_msg_id,
                )
        except Exception as e:
            logger.error("Не удалось переслать TG->VK сообщение (chat_id=%s): %s", tg_chat_id, e)
            return

        if sent_vk_msg_id is not None:
            await add_message_map(vk_peer_id, sent_vk_msg_id, tg_chat_id, message.message_id)
            await add_message_history(
                vk_peer_id, "tg_to_vk", sender_tg_user_id=user_id or None, sender_vk_user_id=None,
                display_name=header, text=(text_body or caption_body), has_media=has_media,
                tg_msg_id=message.message_id, vk_msg_id=sent_vk_msg_id,
            )
            if user_id:
                await bump_user_activity(user_id, vk_peer_id)

    async def _download_tg_file(self, file_id: str) -> Optional[bytes]:
        buf = await self.tg_bot.download(file_id)
        if buf is None:
            return None
        return buf.read()

    async def _forward_tg_voice(
        self, vk_peer_id: int, header: str, file_id: str, reply_to: Optional[int] = None,
    ) -> Optional[int]:
        data = await self._download_tg_file(file_id)
        if data is None:
            return None
        try:
            attachment = await VoiceMessageUploader(self.vk_bot.api).upload(data, peer_id=vk_peer_id)
        except Exception as e:
            logger.error("Не удалось загрузить голосовое TG->VK peer_id=%s: %s", vk_peer_id, e)
            return None
        return await self.vk_sender.send_attachment(vk_peer_id, attachment, header, reply_to=reply_to)

    async def _forward_tg_photo(
        self, vk_peer_id: int, header: str, file_id: str, caption_text: Optional[str], reply_to: Optional[int] = None,
    ) -> Optional[int]:
        data = await self._download_tg_file(file_id)
        if data is None:
            return None
        try:
            attachment = await PhotoMessageUploader(self.vk_bot.api).upload(data, peer_id=vk_peer_id)
        except Exception as e:
            logger.error("Не удалось загрузить фото TG->VK peer_id=%s: %s", vk_peer_id, e)
            return None
        caption = f"{header}:\n{caption_text}" if caption_text else header
        return await self.vk_sender.send_attachment(vk_peer_id, attachment, caption, reply_to=reply_to)

    async def _forward_tg_video(
        self, vk_peer_id: int, header: str, file_id: str, caption_text: Optional[str], reply_to: Optional[int] = None,
    ) -> Optional[int]:
        data = await self._download_tg_file(file_id)
        if data is None:
            return None
        try:
            attachment = await VideoUploader(self.vk_bot.api).upload(
                data, group_id=config.vk_group_id, name=header,
            )
        except Exception as e:
            logger.error("Не удалось загрузить видео TG->VK peer_id=%s: %s", vk_peer_id, e)
            return None
        caption = f"{header}:\n{caption_text}" if caption_text else header
        return await self.vk_sender.send_attachment(vk_peer_id, attachment, caption, reply_to=reply_to)

    # --- Редактирование -----------------------------------------------------

    async def handle_tg_edit(self, bridge: dict, message: TgMessage) -> None:
        """TG edited_message -> VK messages.edit. Синхронизируется только текст."""
        if message.from_user and message.from_user.is_bot:
            return
        if not message.text:
            return

        vk_peer_id = bridge["vk_peer_id"]
        tg_chat_id = bridge["tg_chat_id"]

        row = await get_vk_message(tg_chat_id, message.message_id)
        if row is None:
            return  # редактируемое сообщение не было переслано (или до привязки) - нечего обновлять

        fallback_name = message.from_user.full_name if message.from_user else "TG"
        user_id = message.from_user.id if message.from_user else 0
        header = await _display_name("tg", user_id, fallback_name)

        vk_header = header
        verification = await get_vk_verification(user_id) if user_id else None
        if verification and verification.get("vk_user_id"):
            vk_header = _vk_mention(verification["vk_user_id"], sanitize_display_name(header))

        await self.vk_sender.edit_text(vk_peer_id, row["vk_msg_id"], f"{vk_header}:\n{message.text}")

    async def handle_vk_edit(self, bridge: dict, message: VkMessage) -> None:
        """VK message_edit -> tg_bot.edit_message_text. Синхронизируется только текст."""
        if message.from_id < 0:
            return
        if not message.text:
            return

        vk_peer_id = bridge["vk_peer_id"]
        tg_chat_id = bridge["tg_chat_id"]

        row = await get_tg_message(vk_peer_id, message.id)
        if row is None:
            return

        fallback_name = await _resolve_vk_name(self.vk_bot, message.from_id)
        header = await _display_name("vk", message.from_id, fallback_name)
        text = _truncate(f"{header}:\n{message.text}", TG_TEXT_LIMIT)

        try:
            await _call_tg(lambda: self.tg_bot.edit_message_text(
                chat_id=tg_chat_id, message_id=row["tg_msg_id"], text=text,
            ))
        except Exception as e:
            # Частый легитимный случай: сообщение отредактировано на тот же текст
            # ("message is not modified") или исходное сообщение с тех пор удалено
            # в TG вручную ("message to edit not found"): в обоих случаях синхронизировать
            # нечего, это не сбой маршрутизации.
            logger.warning("Не удалось отредактировать сообщение TG chat_id=%s: %s", tg_chat_id, e)

    # --- Удаление -------------------------------------------------------------
    # ПРИМЕЧАНИЕ: ни VK Bots LongPoll (GroupEventType не содержит события
    # удаления обычных сообщений бесед - есть только для комментариев/wall/board),
    # ни Telegram Bot API (нет update-типа на удаление сообщения пользователем)
    # не уведомляют бота об удалении - поэтому эти методы никуда не подключены
    # автоматически (см. STATE.md). Оставлены как готовая к использованию
    # возможность - например, под будущую явную команду в духе "удалить это
    # сообщение с обеих сторон".

    async def handle_vk_delete(self, bridge: dict, vk_msg_id: int) -> bool:
        """Удаляет в TG сообщение, соответствующее vk_msg_id в этом мосте."""
        tg_chat_id = bridge["tg_chat_id"]
        row = await get_tg_message(bridge["vk_peer_id"], vk_msg_id)
        if row is None:
            return False
        try:
            return await _call_tg(lambda: self.tg_bot.delete_message(
                chat_id=tg_chat_id, message_id=row["tg_msg_id"],
            ))
        except Exception as e:
            logger.error("Не удалось удалить сообщение TG chat_id=%s: %s", tg_chat_id, e)
            return False

    async def handle_tg_delete(self, bridge: dict, tg_msg_id: int) -> bool:
        """Удаляет в VK сообщение, соответствующее tg_msg_id в этом мосте."""
        vk_peer_id = bridge["vk_peer_id"]
        row = await get_vk_message(bridge["tg_chat_id"], tg_msg_id)
        if row is None:
            return False
        return await self.vk_sender.delete_message(vk_peer_id, row["vk_msg_id"])
