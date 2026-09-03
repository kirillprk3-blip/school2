import csv
import io
import logging
import re
from typing import TYPE_CHECKING, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import config
from database import (
    complete_bridge_link,
    create_vk_verification_code,
    delete_bridge,
    get_activity_report,
    get_bridge_by_link_code,
    get_bridge_by_tg_chat,
    get_message_history_by_tg_msg,
    get_user_nickname,
    get_vk_verification,
    save_user_nickname,
    set_user_custom_name,
)

if TYPE_CHECKING:
    from router import MessageRouter

logger = logging.getLogger(__name__)

router = Router()

_message_router: Optional["MessageRouter"] = None
_vk_community_url: Optional[str] = None

ADMIN_URL = "https://t.me/s1hopu"
NICKNAME_MIN_LEN = 2
NICKNAME_MAX_LEN = 32

INSTRUCTIONS_TEXT = (
    "Вот как работает мост между VK и Telegram:\n\n"
    "СНАЧАЛА - подтверди свою страницу VK:\n"
    "Напиши команду /link_vk - бот пришлёт код, отправь его личным сообщением "
    "сообществу VK. Это разовая проверка: без неё бот не даст ни привязать чат, "
    "ни писать в VK - чтобы никто не мог писать в беседу класса под чужим именем.\n\n"
    "ПРИВЯЗАТЬ ГРУППУ Telegram (простой способ, если бот уже добавлен в группу "
    "и уже администратор там):\n"
    "Шаг 1: в беседе VK напиши /link - бот пришлёт код привязки.\n"
    "Шаг 2: отправь этот код обычным сообщением в свою TG-группу (без команд, "
    "без слэша). Готово, группа привязана сама.\n\n"
    "ПРИВЯЗАТЬ ГРУППУ, если бота там ещё нет:\n"
    "Шаг 1: в беседе VK напиши /link - бот пришлёт код и ссылку.\n"
    "Шаг 2: перейди по ссылке в Telegram - откроется выбор группы, выбери "
    "нужную. Бот добавится в неё.\n"
    "Шаг 3: сделай бота администратором этой группы (без этого он не видит "
    "сообщения - ограничение самого Telegram).\n"
    "Шаг 4: если не привязалось само - отправь в группу код из Шага 1 обычным "
    "сообщением (как в простом способе выше).\n\n"
    "ПРИВЯЗАТЬ ЛИЧНЫЙ чат с ботом (вместо группы):\n"
    "В беседе VK напиши команду /link_private - бот пришлёт отдельную ссылку "
    "именно для личного чата.\n\n"
    "Дальше: сообщения из VK будут приходить в Telegram автоматически. Чтобы "
    "отправить что-то из Telegram в VK, напиши в начале сообщения слово vk "
    "(без слэша) - например: vk привет всем (без этого слова бот ничего не "
    "пересылает, чтобы не засорять VK).\n\n"
    "Полезные команды:\n"
    "/link_vk - подтвердить (или переподтвердить) свою страницу VK\n"
    "/status - проверить, к какой беседе VK привязан этот чат\n"
    "/menu - открыть это меню ещё раз в любой момент\n"
    "/unlink - отвязать чат (в группе - только для администраторов)\n"
    "/setname <имя> - задать, каким именем подписывать твои сообщения при пересылке"
)


class RegistrationState(StatesGroup):
    waiting_for_nickname = State()


def set_message_router(message_router: "MessageRouter") -> None:
    """Внедряет маршрутизатор VK<->TG (создаётся в main.py, когда готовы оба бота)."""
    global _message_router
    _message_router = message_router


def set_vk_community_url(url: str) -> None:
    """Внедряет ссылку на сообщество VK (main.py, узнаёт её один раз при старте
    через groups.getById) - используется в подсказке команды /link_vk."""
    global _vk_community_url
    _vk_community_url = url


async def _require_vk_verification(message: Message) -> Optional[dict]:
    """Гейт "сначала подтверди VK": без пройденной верификации подделать
    отправителя в TG тривиально (/setname на любое имя), а VK от подделки
    защищён устройством самого VK API - значит именно TG-сторону нужно
    привязывать к реальному VK-профилю, прежде чем доверять её действиям."""
    verification = await get_vk_verification(message.from_user.id)
    if verification is None:
        await message.answer(
            "Сначала нужно подтвердить свою страницу VK - отправь команду /link_vk"
        )
        return None
    return verification


@router.message(Command("link_vk"))
async def cmd_link_vk(message: Message) -> None:
    code = await create_vk_verification_code(message.from_user.id)
    community_hint = f"нашему сообществу VK: {_vk_community_url}" if _vk_community_url else "нашему сообществу VK"
    await message.answer(
        f"Твой код подтверждения: {code}\n\n"
        f"Отправь его личным сообщением {community_hint} - это докажет, что ты "
        "владеешь именно этой страницей VK. Код действует 15 минут."
    )


def _admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Связаться с админом", url=ADMIN_URL)]],
    )


async def _send_main_menu(message: Message, prefix: str = "") -> None:
    text = f"{prefix}{INSTRUCTIONS_TEXT}" if prefix else INSTRUCTIONS_TEXT
    await message.answer(text, reply_markup=_admin_keyboard())


async def _bot_is_group_admin(message: Message) -> bool:
    """В личных чатах Telegram ничего не скрывает от бота - проверка не нужна.

    В группах/супергруппах, если бот НЕ администратор, Telegram включает
    приватный режим и не доставляет боту обычные текстовые сообщения (только
    команды вида /start) - тогда, например, ответ с никнеймом в состоянии
    waiting_for_nickname бот просто никогда не получит, и переписка молча
    зависнет. Проверяем заранее и предупреждаем, а не оставляем в тишине.
    """
    if message.chat.type == ChatType.PRIVATE:
        return True
    try:
        member = await message.bot.get_chat_member(message.chat.id, message.bot.id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except Exception as e:
        logger.warning("Не удалось проверить права бота в чате %s: %s", message.chat.id, e)
        return True  # не блокируем пользователя из-за сбоя самой проверки


async def _is_chat_admin(message: Message) -> bool:
    """В личном чате пользователь и так управляет только своей привязкой - ограничивать
    нечего. В группе отвязку бесед должен делать только администратор группы, иначе
    любой участник может одной командой сломать пересылку всем остальным.

    В отличие от _bot_is_group_admin, при сбое самой проверки НЕ пропускаем действие
    (fail closed) - это защита деструктивной команды, ошибочное разрешение опаснее
    ошибочного отказа.
    """
    if message.chat.type == ChatType.PRIVATE:
        return True
    try:
        member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except Exception as e:
        logger.warning("Не удалось проверить права пользователя в чате %s: %s", message.chat.id, e)
        return False


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext) -> None:
    if await _require_vk_verification(message) is None:
        return

    link_code = (command.args or "").strip()

    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await _cmd_start_group(message, link_code)
        return

    if not await _bot_is_group_admin(message):
        await message.answer(
            "Сделай меня администратором этой группы. Без этого Telegram скрывает от меня "
            "обычные сообщения (я вижу только команды вроде /start), и бот не сможет ни "
            "спросить у тебя никнейм, ни пересылать переписку.\n\n"
            "Выдай права администратора и напиши /start ещё раз."
        )
        return

    nickname = await get_user_nickname(message.from_user.id)

    if nickname is None:
        if link_code:
            await state.update_data(pending_link_code=link_code)
        await state.set_state(RegistrationState.waiting_for_nickname)
        await message.answer(
            "Привет! Давай знакомиться. Напиши свой никнейм или имя, "
            "которое будет использовать бот:"
        )
        return

    if not link_code:
        await _send_main_menu(message, prefix=f"С возвращением, {nickname}!\n\n")
        return

    bridge = await get_bridge_by_link_code(link_code)
    if bridge is None:
        await message.answer("Код привязки недействителен или уже использован.")
        return

    if not await complete_bridge_link(bridge["vk_peer_id"], message.chat.id):
        await message.answer(
            "Этот чат уже привязан к другой беседе VK. Сначала отправь /unlink, "
            "затем попробуй снова."
        )
        return

    await message.answer(f"Готово, чат привязан к беседе VK (peer_id: {bridge['vk_peer_id']}).")


async def _link_group_by_code(message: Message, link_code: str) -> None:
    """Общая логика привязки группы: проверка прав -> привязка -> ответ.

    Используется и из /start <code> (Deep Link), и из хэндлера "голого" кода,
    отправленного обычным сообщением в группе, где бот уже администратор.
    """
    if await _require_vk_verification(message) is None:
        return

    if not await _is_chat_admin(message):
        await message.answer("Привязывать мост к группе могут только администраторы.")
        return

    if not await _bot_is_group_admin(message):
        await message.answer(
            "Для работы моста сделайте бота администратором группы, "
            "затем повторно отправьте команду /start <code>"
        )
        return

    bridge = await get_bridge_by_link_code(link_code)
    if bridge is None:
        await message.answer("Код привязки недействителен или уже использован.")
        return

    if not await complete_bridge_link(bridge["vk_peer_id"], message.chat.id):
        await message.answer(
            "Эта группа уже привязана к другой беседе VK. Сначала отправьте /unlink, "
            "затем попробуйте снова."
        )
        return

    await message.answer(f"Готово, чат привязан к беседе VK (peer_id: {bridge['vk_peer_id']}).")


async def _cmd_start_group(message: Message, link_code: str) -> None:
    """Привязка группы по Deep Link (?startgroup=<code>).

    Групповой флоу не проходит через регистрацию никнейма: в группе без прав
    администратора Telegram всё равно не доставит боту текстовый ответ
    (privacy mode), поэтому здесь только проверка прав и сразу привязка.
    """
    if not link_code:
        await message.answer(
            "Чтобы привязать эту группу к VK: напиши любое сообщение в нужной "
            "беседе VK, бот пришлёт туда код привязки. Если бот уже добавлен в "
            "эту TG-группу и уже администратор здесь - просто отправь сюда этот "
            "код обычным сообщением, без команд. Если бота ещё нет в группе - "
            "перейди по ссылке из того же сообщения ВК, она сама добавит его."
        )
        return

    await _link_group_by_code(message, link_code)


_GROUP_LINK_CODE_RE = re.compile(r"^[A-Za-z0-9]{6}$")


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}) & F.text.regexp(_GROUP_LINK_CODE_RE))
async def cmd_group_bare_code(message: Message) -> None:
    """Привязка группы обычным сообщением с кодом, без /start.

    Работает только если бот уже администратор группы - иначе Telegram из-за
    privacy mode вообще не доставит боту это сообщение, и хэндлер не сработает.
    Если текст похож на код, но такого моста нет (случайное совпадение или
    код уже использован) - молчим, не мешаем обычной переписке в группе.
    """
    code = message.text.strip().upper()
    bridge = await get_bridge_by_link_code(code)
    if bridge is None:
        return

    await _link_group_by_code(message, code)


@router.message(RegistrationState.waiting_for_nickname)
async def process_nickname(message: Message, state: FSMContext) -> None:
    nickname = (message.text or "").strip()

    if not (NICKNAME_MIN_LEN <= len(nickname) <= NICKNAME_MAX_LEN):
        await message.answer(
            f"Никнейм должен быть от {NICKNAME_MIN_LEN} до {NICKNAME_MAX_LEN} символов. "
            "Попробуй ещё раз:"
        )
        return

    await save_user_nickname(message.from_user.id, nickname)

    data = await state.get_data()
    pending_code = data.get("pending_link_code")
    await state.clear()

    link_note = ""
    if pending_code:
        bridge = await get_bridge_by_link_code(pending_code)
        if bridge is not None and await complete_bridge_link(bridge["vk_peer_id"], message.chat.id):
            link_note = f"Заодно привязал этот чат к беседе VK (peer_id: {bridge['vk_peer_id']}).\n\n"
        elif bridge is not None:
            link_note = (
                "Кстати, этот чат уже привязан к другой беседе VK - код привязки "
                "использовать не удалось. Если нужно, сначала отправь /unlink.\n\n"
            )
        else:
            link_note = (
                "Кстати, код привязки, с которым ты пришёл, уже недействителен. "
                "Если нужно, привяжи чат заново.\n\n"
            )

    await message.answer(f"Отлично, {nickname}! Буду звать тебя так.")
    await _send_main_menu(message, prefix=link_note)


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    bridge = await get_bridge_by_tg_chat(message.chat.id)

    if bridge is None:
        await message.answer("Статус: не привязан к VK.")
        return

    await message.answer(f"Статус: привязан к VK Peer ID: {bridge['vk_peer_id']}")


@router.message(Command("unlink"))
async def cmd_unlink(message: Message) -> None:
    if not await _is_chat_admin(message):
        await message.answer("Отвязать чат может только администратор группы.")
        return

    bridge = await get_bridge_by_tg_chat(message.chat.id)

    if bridge is None:
        await message.answer("Этот чат не привязан ни к одной беседе VK.")
        return

    await delete_bridge(bridge["vk_peer_id"])
    await message.answer("Привязка удалена.")


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    """Лёгкая панель управления: текущий статус + инструкция + кнопка админа."""
    bridge = await get_bridge_by_tg_chat(message.chat.id)
    status_line = (
        f"Статус: привязан к VK Peer ID {bridge['vk_peer_id']}.\n\n" if bridge
        else "Статус: этот чат пока не привязан к VK.\n\n"
    )
    await _send_main_menu(message, prefix=status_line)


@router.message(Command("whois"))
async def cmd_whois(message: Message) -> None:
    """Для админа группы: кто реально автор пересланного сообщения (ответом на
    него). Проверенное соответствие VK<->TG делает возможным честно ответить
    на этот вопрос, а не гадать по подписи, которую мог подделать /setname."""
    if not await _is_chat_admin(message):
        await message.answer("Команда доступна только администраторам.")
        return

    if message.reply_to_message is None:
        await message.answer("Ответь этой командой на пересланное сообщение, чтобы узнать автора.")
        return

    bridge = await get_bridge_by_tg_chat(message.chat.id)
    if bridge is None:
        await message.answer("Этот чат не привязан ни к одной беседе VK.")
        return

    record = await get_message_history_by_tg_msg(bridge["vk_peer_id"], message.reply_to_message.message_id)
    if record is None:
        await message.answer("Не нашёл информацию об авторе этого сообщения.")
        return

    if record["sender_vk_user_id"] is not None:
        text = (
            f"Автор - пользователь VK: {record['display_name']}\n"
            f"Профиль: https://vk.com/id{record['sender_vk_user_id']}"
        )
    elif record["sender_tg_user_id"] is not None:
        tg_user_id = record["sender_tg_user_id"]
        verification = await get_vk_verification(tg_user_id)
        lines = [
            f"Автор - пользователь Telegram (ID: {tg_user_id})",
            f"Отображаемое имя на момент отправки: {record['display_name']}",
        ]
        if verification and verification.get("vk_profile_url"):
            lines.append(f"Подтверждённая страница VK: https://{verification['vk_profile_url']}")
        else:
            lines.append("Страница VK не подтверждена (сообщение отправлено до введения проверки).")
        text = "\n".join(lines)
    else:
        text = f"Автор неизвестен. Отображаемое имя: {record['display_name']}"

    await message.answer(text)


def _csv_safe(value: str) -> str:
    """Защита от CSV-injection: ячейка, начинающаяся с =+-@, может быть
    воспринята Excel/Sheets как формула при открытии файла."""
    if value and value[0] in "=+-@":
        return "'" + value
    return value


def _build_activity_csv(rows: list) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Имя", "Профиль VK", "Верифицирован", "Сообщений", "Последнее сообщение"])
    for row in rows:
        writer.writerow([
            _csv_safe(row["display_name"]),
            f"https://{row['vk_profile_url']}" if row["vk_profile_url"] else "",
            row["verified_at"] or "",
            row["message_count"],
            row["last_message_at"] or "",
        ])
    return buf.getvalue().encode("utf-8-sig")


@router.message(Command("report"))
async def cmd_report(message: Message) -> None:
    """Для админа группы: сводка активности + CSV со всеми, кто пересылал
    сообщения TG->VK в этом мосте - подтверждённые VK-профили, число сообщений."""
    if not await _is_chat_admin(message):
        await message.answer("Команда доступна только администраторам.")
        return

    bridge = await get_bridge_by_tg_chat(message.chat.id)
    if bridge is None:
        await message.answer("Этот чат не привязан ни к одной беседе VK.")
        return

    rows = await get_activity_report(bridge["vk_peer_id"])
    if not rows:
        await message.answer("Пока нет ни одного пересланного сообщения для отчёта.")
        return

    total_messages = sum(row["message_count"] for row in rows)
    top_lines = "\n".join(
        f"{i + 1}. {row['display_name']} - {row['message_count']}"
        for i, row in enumerate(rows[:5])
    )
    summary = (
        f"Участников: {len(rows)}\n"
        f"Сообщений отправлено в VK: {total_messages}\n\n"
        f"Топ по активности:\n{top_lines}"
    )
    await message.answer(summary)

    csv_bytes = _build_activity_csv(rows)
    await message.answer_document(BufferedInputFile(csv_bytes, filename="report.csv"))


def _setname_profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Использовать из профиля", callback_data="setname_use_profile"),
        ]],
    )


@router.message(Command("setname"))
async def cmd_setname(message: Message, command: CommandObject) -> None:
    new_name = (command.args or "").strip()

    if not new_name:
        await message.answer(
            "Выберите способ установки имени или введите его ручным вводом: /setname <Имя>",
            reply_markup=_setname_profile_keyboard(),
        )
        return

    if len(new_name) > NICKNAME_MAX_LEN:
        await message.answer(f"Имя не должно быть длиннее {NICKNAME_MAX_LEN} символов.")
        return

    await set_user_custom_name(str(message.from_user.id), new_name)
    await message.answer(f"Имя сохранено: {new_name}")


@router.callback_query(F.data == "setname_use_profile")
async def cb_setname_use_profile(callback: CallbackQuery) -> None:
    verification = await get_vk_verification(callback.from_user.id)
    if verification and verification.get("vk_full_name"):
        source_name = verification["vk_full_name"]
    else:
        source_name = callback.from_user.full_name or callback.from_user.first_name or "Без имени"
    name = source_name[:NICKNAME_MAX_LEN]

    await set_user_custom_name(str(callback.from_user.id), name)
    await save_user_nickname(callback.from_user.id, name)
    await callback.answer()

    if callback.message is not None:
        await callback.message.edit_text(f"Установлено имя: {name}")
    else:
        await callback.bot.send_message(callback.from_user.id, f"Установлено имя: {name}")


_VK_TRIGGER_RE = re.compile(r"^vk(?:\s+(?P<body>.*))?$", re.IGNORECASE | re.DOTALL)


def _strip_vk_trigger(text: Optional[str]) -> Optional[str]:
    """Возвращает текст после префикса "vk" (без слэша), либо None, если его нет.

    Разделитель между "vk" и текстом - любой пробельный символ, включая перенос
    строки (`vk\\nТекст`). Слова вида "vkontakte"/"vka" не матчатся - после "vk"
    обязателен либо пробельный символ, либо конец строки. Пустая строка после
    отрезания - валидный результат (просто "vk" без текста).
    """
    if not text:
        return None

    match = _VK_TRIGGER_RE.match(text)
    if not match:
        return None

    return (match.group("body") or "").strip()


@router.message(F.text | F.voice | F.photo | F.video)
async def forward_to_vk(message: Message) -> None:
    """Пересылает в VK через MessageRouter, но только если сообщение/подпись
    начинается с "vk" (регистронезависимо, без слэша)."""
    has_media = bool(message.voice or message.photo or message.video)
    source_text = message.caption if has_media else message.text

    stripped = _strip_vk_trigger(source_text)
    if stripped is None:
        return  # префикса "vk" нет - ничего не пересылаем

    if not stripped and not has_media:
        await message.answer("Использование: vk <текст сообщения>")
        return

    if _message_router is None:
        logger.warning("MessageRouter не инициализирован, сообщение не переслано в VK.")
        return

    bridge = await get_bridge_by_tg_chat(message.chat.id)
    if bridge is None:
        return

    verification = await _require_vk_verification(message)
    if verification is None:
        return

    is_member = await _message_router.is_sender_in_conversation(
        bridge["vk_peer_id"], verification["vk_user_id"],
    )
    if not is_member:
        await message.answer("Ты не участник этой VK-беседы, поэтому переслать нельзя.")
        return

    await _message_router.handle_tg_message(bridge, message, text_override=stripped)


@router.edited_message(F.text)
async def forward_edit_to_vk(message: Message) -> None:
    """Пересылает редактирование текстового сообщения привязанного чата в VK."""
    if _message_router is None:
        return

    bridge = await get_bridge_by_tg_chat(message.chat.id)
    if bridge is None:
        return

    await _message_router.handle_tg_edit(bridge, message)


def create_bot() -> Bot:
    if config.tg_api_base_url:
        session = AiohttpSession(api=TelegramAPIServer.from_base(config.tg_api_base_url))
        return Bot(token=config.tg_bot_token, session=session)
    return Bot(token=config.tg_bot_token)


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    return dp
