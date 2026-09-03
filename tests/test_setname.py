from unittest.mock import AsyncMock, MagicMock

from aiogram.filters import CommandObject

import database as db
import tg_handlers as th
from conftest import make_message


def make_callback(user_id=999, full_name="Кирилл Иванов", first_name="Кирилл"):
    cb = MagicMock()
    cb.from_user.id = user_id
    cb.from_user.full_name = full_name
    cb.from_user.first_name = first_name
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.bot.send_message = AsyncMock()
    return cb


# --- /setname без аргумента: клавиатура --------------------------------------

async def test_setname_without_args_shows_profile_button(temp_db):
    msg = make_message(user_id=999)
    await th.cmd_setname(msg, CommandObject(prefix="/", command="setname", args=None))

    assert "Выберите способ установки имени" in msg.answer.call_args.args[0]
    keyboard = msg.answer.call_args.kwargs["reply_markup"]
    button = keyboard.inline_keyboard[0][0]
    assert button.text == "Использовать из профиля"
    assert button.callback_data == "setname_use_profile"


# --- /setname <Имя>: ручной ввод, лимит 32 символа ---------------------------

async def test_setname_manual_input_within_limit(temp_db):
    msg = make_message(user_id=999)
    await th.cmd_setname(msg, CommandObject(prefix="/", command="setname", args="Кирилл"))

    assert "сохранено" in msg.answer.call_args.args[0]
    assert (await db.get_user_settings("999"))["custom_name"] == "Кирилл"
    # ручной ввод не трогает users.nickname
    assert await db.get_user_nickname(999) is None


async def test_setname_manual_input_over_limit_rejected(temp_db):
    msg = make_message(user_id=999)
    too_long = "Ф" * (th.NICKNAME_MAX_LEN + 1)
    await th.cmd_setname(msg, CommandObject(prefix="/", command="setname", args=too_long))

    assert "не должно быть длиннее" in msg.answer.call_args.args[0]
    assert await db.get_user_settings("999") is None


# --- callback setname_use_profile --------------------------------------------

async def test_setname_use_profile_saves_and_truncates(temp_db):
    long_name = "И" * 50
    cb = make_callback(user_id=999, full_name=long_name)

    await th.cb_setname_use_profile(cb)

    cb.answer.assert_called_once()
    saved = (await db.get_user_settings("999"))["custom_name"]
    assert saved == long_name[: th.NICKNAME_MAX_LEN]
    assert len(saved) == th.NICKNAME_MAX_LEN
    assert await db.get_user_nickname(999) == saved
    cb.message.edit_text.assert_called_once()
    assert "Установлено имя" in cb.message.edit_text.call_args.args[0]
    assert saved in cb.message.edit_text.call_args.args[0]


async def test_setname_use_profile_falls_back_to_first_name(temp_db):
    cb = make_callback(user_id=999, full_name=None, first_name="Аня")

    await th.cb_setname_use_profile(cb)

    assert (await db.get_user_settings("999"))["custom_name"] == "Аня"
