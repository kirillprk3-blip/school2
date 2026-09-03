from unittest.mock import AsyncMock

from aiogram.filters import CommandObject
from vkbottle.bot import Bot as VkBot

import database as db
import tg_handlers as th
import vk_listener as vl
from conftest import make_message, make_state, verify_user


def _member(status):
    m = AsyncMock()
    m.status = status
    return m


def _new_message_update(peer_id=100, text=""):
    return {
        "type": "message_new",
        "object": {"message": {
            "id": 1, "date": 0, "from_id": peer_id, "peer_id": peer_id,
            "out": False, "text": text, "conversation_message_id": 1,
            "version": 1, "random_id": 0, "attachments": [], "important": False,
            "is_hidden": False, "fwd_messages": [],
        }},
        "group_id": 1,
    }


# --- vk_listener.py: формат Deep Link ----------------------------------------

async def test_default_link_is_group_deep_link(temp_db):
    bot = VkBot(token="test_vk_token")
    sent = []

    async def fake_request(method, params=None, **kwargs):
        sent.append(params or {})
        return {"response": 1}

    bot.api.request = fake_request
    vl.VKListener(bot=bot, tg_bot_username="test_bot")

    await bot.router.route(_new_message_update(peer_id=200, text="привет"), bot.api)

    assert len(sent) == 1
    assert "https://t.me/test_bot?startgroup=" in sent[0]["message"]
    assert "?start=" not in sent[0]["message"].replace("?startgroup=", "")

    await bot.api.http_client.close()


async def test_link_private_command_sends_private_link(temp_db):
    bot = VkBot(token="test_vk_token")
    sent = []

    async def fake_request(method, params=None, **kwargs):
        sent.append(params or {})
        return {"response": 1}

    bot.api.request = fake_request
    vl.VKListener(bot=bot, tg_bot_username="test_bot")

    # первое сообщение - обычная (групповая) ссылка
    await bot.router.route(_new_message_update(peer_id=300, text="привет"), bot.api)
    assert "startgroup=" in sent[0]["message"]

    # явная команда /link_private - личная ссылка
    await bot.router.route(_new_message_update(peer_id=300, text="/link_private"), bot.api)
    assert len(sent) == 2
    assert "https://t.me/test_bot?start=" in sent[1]["message"]
    assert "startgroup" not in sent[1]["message"]

    await bot.api.http_client.close()


# --- tg_handlers.py: /start <code> в группе -----------------------------------

async def test_group_start_without_code_shows_hint(temp_db):
    await verify_user()
    msg = make_message(chat_type="group")
    state = make_state()

    await th.cmd_start(msg, CommandObject(prefix="/", command="start", args=None), state)

    assert "ссылк" in msg.answer.call_args.args[0]
    msg.bot.get_chat_member.assert_not_called()


async def test_group_start_blocked_for_non_admin_user(temp_db):
    await verify_user()
    await db.create_bridge(555, link_code="ABC123")
    msg = make_message(chat_type="group", chat_id=100)
    state = make_state()

    async def get_chat_member(chat_id, user_id):
        # from_user.id == 999 (обычный участник); bot.id == 123456
        return _member("member")

    msg.bot.get_chat_member = AsyncMock(side_effect=get_chat_member)

    await th.cmd_start(msg, CommandObject(prefix="/", command="start", args="ABC123"), state)

    assert "администратор" in msg.answer.call_args.args[0]
    bridge = await db.get_bridge_by_vk_peer(555)
    assert bridge["tg_chat_id"] is None  # привязка не произошла


async def test_group_start_blocked_when_bot_not_admin(temp_db):
    await verify_user()
    await db.create_bridge(555, link_code="ABC123")
    msg = make_message(chat_type="group", chat_id=100)
    state = make_state()

    async def get_chat_member(chat_id, user_id):
        # пользователь (999) - админ, бот (123456) - нет
        return _member("administrator") if user_id == 999 else _member("member")

    msg.bot.get_chat_member = AsyncMock(side_effect=get_chat_member)

    await th.cmd_start(msg, CommandObject(prefix="/", command="start", args="ABC123"), state)

    assert "администратором группы" in msg.answer.call_args.args[0]
    bridge = await db.get_bridge_by_vk_peer(555)
    assert bridge["tg_chat_id"] is None


async def test_group_start_succeeds_when_both_admins(temp_db):
    await verify_user()
    await db.create_bridge(555, link_code="ABC123")
    msg = make_message(chat_type="group", chat_id=100)
    state = make_state()

    msg.bot.get_chat_member = AsyncMock(return_value=_member("administrator"))

    await th.cmd_start(msg, CommandObject(prefix="/", command="start", args="ABC123"), state)

    assert "привязан" in msg.answer.call_args.args[0]
    bridge = await db.get_bridge_by_vk_peer(555)
    assert bridge == {"vk_peer_id": 555, "tg_chat_id": 100, "link_code": None}
    # регистрация никнейма в группах не запускается
    assert await state.get_state() is None
    assert await db.get_user_nickname(999) is None


async def test_group_start_chat_already_bound_elsewhere(temp_db):
    """Регрессия: раньше это падало непойманным IntegrityError вместо ответа."""
    await verify_user()
    await db.create_bridge(111, tg_chat_id=100)  # чат 100 уже занят другим мостом
    await db.create_bridge(555, link_code="ABC123")
    msg = make_message(chat_type="group", chat_id=100)
    state = make_state()
    msg.bot.get_chat_member = AsyncMock(return_value=_member("administrator"))

    await th.cmd_start(msg, CommandObject(prefix="/", command="start", args="ABC123"), state)

    assert "уже привязан" in msg.answer.call_args.args[0]
    bridge = await db.get_bridge_by_vk_peer(555)
    assert bridge["tg_chat_id"] is None


async def test_group_start_invalid_code(temp_db):
    await verify_user()
    msg = make_message(chat_type="group", chat_id=100)
    state = make_state()
    msg.bot.get_chat_member = AsyncMock(return_value=_member("administrator"))

    await th.cmd_start(msg, CommandObject(prefix="/", command="start", args="badcode"), state)

    assert "недействителен" in msg.answer.call_args.args[0]


# --- tg_handlers.py: "голый" код без /start в группе ---------------------------

async def test_bare_code_in_group_links_when_admin(temp_db):
    await verify_user()
    await db.create_bridge(555, link_code="AB12CD")
    msg = make_message(chat_type="group", chat_id=100)
    msg.text = "AB12CD"
    msg.bot.get_chat_member = AsyncMock(return_value=_member("administrator"))

    await th.cmd_group_bare_code(msg)

    assert "привязан" in msg.answer.call_args.args[0]
    bridge = await db.get_bridge_by_vk_peer(555)
    assert bridge == {"vk_peer_id": 555, "tg_chat_id": 100, "link_code": None}


async def test_bare_code_in_group_is_case_insensitive(temp_db):
    await verify_user()
    await db.create_bridge(555, link_code="AB12CD")
    msg = make_message(chat_type="group", chat_id=100)
    msg.text = "ab12cd"
    msg.bot.get_chat_member = AsyncMock(return_value=_member("administrator"))

    await th.cmd_group_bare_code(msg)

    assert "привязан" in msg.answer.call_args.args[0]


async def test_bare_code_in_group_blocked_for_non_admin(temp_db):
    await verify_user()
    await db.create_bridge(555, link_code="AB12CD")
    msg = make_message(chat_type="group", chat_id=100)
    msg.text = "AB12CD"
    msg.bot.get_chat_member = AsyncMock(return_value=_member("member"))

    await th.cmd_group_bare_code(msg)

    assert "администратор" in msg.answer.call_args.args[0]
    bridge = await db.get_bridge_by_vk_peer(555)
    assert bridge["tg_chat_id"] is None


async def test_bare_code_that_does_not_match_any_bridge_is_silently_ignored(temp_db):
    msg = make_message(chat_type="group", chat_id=100)
    msg.text = "ZZ99ZZ"

    await th.cmd_group_bare_code(msg)

    msg.answer.assert_not_called()
    msg.bot.get_chat_member.assert_not_called()  # даже до проверки прав не дошло


async def test_random_group_text_does_not_match_bare_code_filter():
    """Фильтр хэндлера пропускает только строки ровно из 6 букв/цифр."""
    from aiogram import F

    filt = F.chat.type.in_({"group", "supergroup"}) & F.text.regexp(th._GROUP_LINK_CODE_RE)

    msg = make_message(chat_type="group", chat_id=100)
    msg.text = "привет всем, как дела?"
    assert not filt.resolve(msg)

    msg.text = "AB12CD"
    assert filt.resolve(msg)

    msg.text = "AB12CD3"  # 7 символов - не подходит
    assert not filt.resolve(msg)
