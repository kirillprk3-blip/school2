import logging
import random
import re
import secrets
import string
from typing import Awaitable, Callable, Optional

from vkbottle.bot import Bot, Message
from vkbottle_types.events import GroupEventType

from database import (
    confirm_vk_verification,
    create_bridge,
    get_bridge_by_vk_peer,
    get_verification_by_code,
    sanitize_display_name,
)

logger = logging.getLogger(__name__)

_GROUP_LINK_COMMANDS = {"/link", "/start"}
_PRIVATE_LINK_COMMANDS = {"/link_private"}

# Формат кода верификации из database.create_vk_verification_code(): "V" + 8
# буквенно-цифровых символов, например "VA1B2C3D4".
_VERIFICATION_CODE_RE = re.compile(r"^V[A-Z0-9]{8}$")


def generate_link_code() -> str:
    """Случайный 6-значный буквенно-цифровой код привязки (например, A1B2C3).

    secrets.choice(), а не random.choices() - код привязки определяет, к какому
    именно VK/TG-чату получит доступ тот, кто его введёт, значит это
    identity-токен, а не просто dedup-метка.
    """
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(6))


class VKListener:
    """VK Group Bots LongPoll листенер (vkbottle) для мультитенентных мостов.

    Для каждой входящей VK-беседы без активного моста создаёт/переиспользует
    `link_code` и присылает ссылку на привязку TG-чата. Для уже привязанных
    мостов передаёт сообщение дальше через `on_bridged_message` (пересылка,
    router.py, Этап 4), а редактирование VK-сообщений - через
    `on_edit_message` (Этап 5). Удаление сообщений VK Bots LongPoll не
    отдаёт как событие для обычных бесед, поэтому здесь не обрабатывается
    (см. STATE.md).
    """

    def __init__(
        self,
        bot: Bot,
        tg_bot_username: str,
        on_bridged_message: Optional[Callable[[dict, Message], Awaitable[None]]] = None,
        on_edit_message: Optional[Callable[[dict, Message], Awaitable[None]]] = None,
        on_vk_verified: Optional[Callable[[int, str, str], Awaitable[None]]] = None,
    ):
        self.bot = bot
        self.tg_bot_username = tg_bot_username
        self.on_bridged_message = on_bridged_message
        self.on_edit_message = on_edit_message
        self.on_vk_verified = on_vk_verified
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self.bot.on.message()
        async def handle_message(message: Message) -> None:
            await self._handle(message)

        @self.bot.on.raw_event(GroupEventType.MESSAGE_EDIT)
        async def handle_edit(event: dict) -> None:
            message = Message.model_validate(event["object"]["message"])
            await self._handle_edit(message)

    async def _handle(self, message: Message) -> None:
        if message.out:
            return

        # Личное сообщение сообществу (peer_id == from_id, а не беседа): если это
        # похоже на код верификации из /link_vk - обрабатываем его здесь и не идём
        # дальше в обычную логику создания моста (личное сообщение от случайного
        # человека не должно плодить мусорные записи в bridges).
        if message.peer_id == message.from_id:
            code = (message.text or "").strip().upper()
            if _VERIFICATION_CODE_RE.match(code):
                await self._try_confirm_verification(message, code)
                return

        peer_id = message.peer_id
        bridge = await get_bridge_by_vk_peer(peer_id)
        is_new_peer = bridge is None

        if bridge is None or bridge.get("tg_chat_id") is None:
            link_code = bridge["link_code"] if bridge and bridge.get("link_code") else None

            if link_code is None:
                link_code = generate_link_code()
                await create_bridge(peer_id, link_code=link_code)
                logger.info("Создан новый мост vk_peer_id=%s, link_code=%s", peer_id, link_code)

            text = (message.text or "").strip().lower()
            is_private_link_command = text in _PRIVATE_LINK_COMMANDS
            is_group_link_command = text in _GROUP_LINK_COMMANDS

            # По умолчанию (явная команда /link, /start или первое обращение из
            # новой беседы) шлём ссылку для добавления бота сразу в группу Telegram -
            # именно так обходится приватный режим Telegram, из-за которого бот не
            # видит обычные сообщения без прав администратора. Отдельная команда
            # /link_private нужна, только если требуется привязать личный чат.
            if is_private_link_command:
                link_url = f"https://t.me/{self.tg_bot_username}?start={link_code}"
                await self.bot.api.messages.send(
                    peer_id=peer_id,
                    message=f"Для привязки личного чата Telegram перейдите по ссылке: {link_url}",
                    random_id=random.randint(1, 2_147_483_647),
                )
            elif is_group_link_command or is_new_peer:
                link_url = f"https://t.me/{self.tg_bot_username}?startgroup={link_code}"
                # Код показываем первой строкой отдельно: если бот уже добавлен
                # в нужную TG-группу и уже администратор там, достаточно просто
                # отправить туда этот код обычным сообщением - без /start и
                # без перехода по ссылке. Ссылка нужна только тем, кто ещё не
                # добавил бота в группу.
                await self.bot.api.messages.send(
                    peer_id=peer_id,
                    message=(
                        f"Код привязки: {link_code}\n\n"
                        "Если бот уже в вашей TG-группе и уже администратор там - "
                        "просто отправьте туда этот код обычным сообщением, привяжется "
                        "само.\n\n"
                        "Если бота там ещё нет - перейдите по ссылке, чтобы добавить его "
                        f"в группу: {link_url}"
                    ),
                    random_id=random.randint(1, 2_147_483_647),
                )
            return

        if self.on_bridged_message is not None:
            await self.on_bridged_message(bridge, message)

    async def _try_confirm_verification(self, message: Message, code: str) -> None:
        """Подтверждает код из /link_vk (tg_handlers.py), присланный личным
        сообщением сообществу. Если код не найден/просрочен - молча ничего не
        делаем: не подтверждаем и не опровергаем существование чужого кода."""
        verification = await get_verification_by_code(code)
        if verification is None:
            return

        try:
            users = await self.bot.api.users.get(user_ids=[message.from_id])
            full_name = f"{users[0].first_name} {users[0].last_name}".strip() if users else str(message.from_id)
        except Exception as e:
            logger.warning("Не удалось получить имя VK-пользователя %s для верификации: %s", message.from_id, e)
            full_name = str(message.from_id)

        clean_name = sanitize_display_name(full_name)
        profile_url = f"vk.com/id{message.from_id}"

        await confirm_vk_verification(
            tg_user_id=verification["tg_user_id"], vk_user_id=message.from_id,
            vk_profile_url=profile_url, vk_full_name=clean_name,
        )

        await self.bot.api.messages.send(
            peer_id=message.peer_id,
            message="Готово, профиль подтверждён.",
            random_id=random.randint(1, 2_147_483_647),
        )

        if self.on_vk_verified is not None:
            try:
                await self.on_vk_verified(verification["tg_user_id"], clean_name, profile_url)
            except Exception as e:
                logger.warning("on_vk_verified колбэк упал: %s", e)

    async def _handle_edit(self, message: Message) -> None:
        if message.out or self.on_edit_message is None:
            return

        bridge = await get_bridge_by_vk_peer(message.peer_id)
        if bridge is None or bridge.get("tg_chat_id") is None:
            return  # мост ещё не активен, синхронизировать редактирование некуда

        await self.on_edit_message(bridge, message)
