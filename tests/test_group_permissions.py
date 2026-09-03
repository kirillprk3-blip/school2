from unittest.mock import AsyncMock

from aiogram.filters import CommandObject

import database as db
import tg_handlers as th
from conftest import make_message, make_state, verify_user


def _member(status):
    m = AsyncMock()
    m.status = status
    return m


# --- _bot_is_group_admin -----------------------------------------------------

async def test_bot_is_group_admin_always_true_in_private_chat():
    msg = make_message(chat_type="private")
    assert await th._bot_is_group_admin(msg) is True
    msg.bot.get_chat_member.assert_not_called()


async def test_bot_is_group_admin_true_when_bot_is_administrator():
    msg = make_message(chat_type="supergroup")
    msg.bot.get_chat_member.return_value = _member("administrator")
    assert await th._bot_is_group_admin(msg) is True


async def test_bot_is_group_admin_true_when_bot_is_creator():
    msg = make_message(chat_type="group")
    msg.bot.get_chat_member.return_value = _member("creator")
    assert await th._bot_is_group_admin(msg) is True


async def test_bot_is_group_admin_false_when_bot_is_plain_member():
    msg = make_message(chat_type="group")
    msg.bot.get_chat_member.return_value = _member("member")
    assert await th._bot_is_group_admin(msg) is False


async def test_bot_is_group_admin_fails_open_on_api_error():
    """Сбой самой проверки (например, сеть) не должен блокировать пользователя -
    это не защита от чего-то опасного, просто предупреждение."""
    msg = make_message(chat_type="group")
    msg.bot.get_chat_member.side_effect = RuntimeError("network down")
    assert await th._bot_is_group_admin(msg) is True


# --- /start в группе без кода (без прав администратора у бота роли не играет,
#     регистрация никнейма в группах больше не запускается вовсе) -------------

async def test_start_in_group_without_code_asks_for_link(temp_db):
    await verify_user()
    msg = make_message(chat_type="group")
    state = make_state()

    await th.cmd_start(msg, CommandObject(prefix="/", command="start", args=None), state)

    assert "ссылк" in msg.answer.call_args.args[0]
    assert await state.get_state() is None  # регистрация никнейма в группах не запускается
    assert await db.get_user_nickname(999) is None
    msg.bot.get_chat_member.assert_not_called()  # до проверки прав дело не дошло


# --- _is_chat_admin -----------------------------------------------------------

async def test_is_chat_admin_always_true_in_private_chat():
    msg = make_message(chat_type="private")
    assert await th._is_chat_admin(msg) is True


async def test_is_chat_admin_true_for_group_admin():
    msg = make_message(chat_type="supergroup")
    msg.bot.get_chat_member.return_value = _member("administrator")
    assert await th._is_chat_admin(msg) is True


async def test_is_chat_admin_false_for_plain_member():
    msg = make_message(chat_type="supergroup")
    msg.bot.get_chat_member.return_value = _member("member")
    assert await th._is_chat_admin(msg) is False


async def test_is_chat_admin_fails_closed_on_api_error():
    """В отличие от _bot_is_group_admin, здесь ошибка проверки -> отказ (fail closed),
    т.к. это защита деструктивной команды."""
    msg = make_message(chat_type="group")
    msg.bot.get_chat_member.side_effect = RuntimeError("network down")
    assert await th._is_chat_admin(msg) is False


# --- /unlink ограничен админами в группах ------------------------------------

async def test_unlink_blocked_for_regular_group_member(temp_db):
    await db.create_bridge(555, tg_chat_id=100)
    msg = make_message(chat_id=100, chat_type="group")
    msg.bot.get_chat_member.return_value = _member("member")

    await th.cmd_unlink(msg)

    assert "администратор" in msg.answer.call_args.args[0]
    assert await db.get_bridge_by_vk_peer(555) is not None  # мост не тронут


async def test_unlink_allowed_for_group_admin(temp_db):
    await db.create_bridge(555, tg_chat_id=100)
    msg = make_message(chat_id=100, chat_type="group")
    msg.bot.get_chat_member.return_value = _member("administrator")

    await th.cmd_unlink(msg)

    assert "удалена" in msg.answer.call_args.args[0]
    assert await db.get_bridge_by_vk_peer(555) is None


async def test_unlink_allowed_in_private_chat_without_check(temp_db):
    await db.create_bridge(555, tg_chat_id=100)
    msg = make_message(chat_id=100, chat_type="private")

    await th.cmd_unlink(msg)

    assert "удалена" in msg.answer.call_args.args[0]
    msg.bot.get_chat_member.assert_not_called()


# --- /menu --------------------------------------------------------------------

async def test_menu_shows_linked_status(temp_db):
    await db.create_bridge(555, tg_chat_id=100)
    msg = make_message(chat_id=100)

    await th.cmd_menu(msg)

    text = msg.answer.call_args.args[0]
    assert "555" in text
    assert "Шаг 1" in text
    assert msg.answer.call_args.kwargs["reply_markup"] is not None


async def test_menu_shows_unlinked_status(temp_db):
    msg = make_message(chat_id=999)

    await th.cmd_menu(msg)

    text = msg.answer.call_args.args[0]
    assert "не привязан" in text
