from aiogram.filters import CommandObject

import database as db
import tg_handlers as th
from conftest import make_message, make_state, verify_user


async def test_start_without_nickname_prompts_registration(temp_db):
    """/start у ещё не зарегистрированного пользователя всегда уходит в регистрацию,
    даже если передан код привязки (детали в tests/test_registration.py)."""
    await verify_user()
    msg = make_message()
    state = make_state()
    await th.cmd_start(msg, CommandObject(prefix="/", command="start", args=None), state)
    assert "никнейм" in msg.answer.call_args.args[0]
    assert await state.get_state() == th.RegistrationState.waiting_for_nickname.state


async def test_start_without_vk_verification_is_blocked(temp_db):
    msg = make_message()
    state = make_state()
    await th.cmd_start(msg, CommandObject(prefix="/", command="start", args=None), state)
    assert "/link_vk" in msg.answer.call_args.args[0]
    assert await state.get_state() is None


async def test_start_with_invalid_code_after_registration(temp_db):
    await verify_user()
    await db.save_user_nickname(999, "Кирилл")
    msg = make_message()
    state = make_state()
    await th.cmd_start(msg, CommandObject(prefix="/", command="start", args="badcode"), state)
    assert "недействителен" in msg.answer.call_args.args[0]


async def test_start_with_valid_code_links_bridge_after_registration(temp_db):
    await verify_user()
    await db.save_user_nickname(999, "Кирилл")
    await db.create_bridge(555, link_code="OK123")
    msg = make_message(chat_id=100)
    state = make_state(chat_id=100)
    await th.cmd_start(msg, CommandObject(prefix="/", command="start", args="OK123"), state)
    assert "привязан" in msg.answer.call_args.args[0]
    bridge = await db.get_bridge_by_vk_peer(555)
    assert bridge == {"vk_peer_id": 555, "tg_chat_id": 100, "link_code": None}


async def test_start_without_code_shows_main_menu_for_registered_user(temp_db):
    await verify_user()
    await db.save_user_nickname(999, "Кирилл")
    msg = make_message()
    state = make_state()
    await th.cmd_start(msg, CommandObject(prefix="/", command="start", args=None), state)
    text = msg.answer.call_args.args[0]
    assert "Кирилл" in text
    assert "Шаг 1" in text
    assert msg.answer.call_args.kwargs["reply_markup"] is not None


async def test_link_vk_sends_code(temp_db):
    msg = make_message(user_id=999)
    await th.cmd_link_vk(msg)
    text = msg.answer.call_args.args[0]
    assert "код подтверждения" in text
    verification = await db.get_verification_by_code(text.split(": ", 1)[1].split("\n", 1)[0])
    assert verification["tg_user_id"] == 999


async def test_status_linked_and_unlinked(temp_db):
    await db.create_bridge(555, tg_chat_id=100)

    msg = make_message(chat_id=100)
    await th.cmd_status(msg)
    assert "555" in msg.answer.call_args.args[0]

    msg2 = make_message(chat_id=777)
    await th.cmd_status(msg2)
    assert "не привязан" in msg2.answer.call_args.args[0]


async def test_unlink_removes_bridge_entirely(temp_db):
    await db.create_bridge(555, tg_chat_id=100)
    msg = make_message(chat_id=100)
    await th.cmd_unlink(msg)
    assert "удалена" in msg.answer.call_args.args[0]
    assert await db.get_bridge_by_vk_peer(555) is None

    msg2 = make_message(chat_id=100)
    await th.cmd_unlink(msg2)
    assert "не привязан" in msg2.answer.call_args.args[0]


async def test_setname_empty_and_with_value(temp_db):
    msg = make_message(user_id=999)
    await th.cmd_setname(msg, CommandObject(prefix="/", command="setname", args=None))
    assert "Выберите способ" in msg.answer.call_args.args[0]
    keyboard = msg.answer.call_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].callback_data == "setname_use_profile"

    msg2 = make_message(user_id=999)
    await th.cmd_setname(msg2, CommandObject(prefix="/", command="setname", args="Кирилл"))
    assert "сохранено" in msg2.answer.call_args.args[0]
    assert (await db.get_user_settings("999"))["custom_name"] == "Кирилл"


async def test_setname_too_long_is_rejected(temp_db):
    msg = make_message(user_id=999)
    too_long = "Ф" * (th.NICKNAME_MAX_LEN + 1)
    await th.cmd_setname(msg, CommandObject(prefix="/", command="setname", args=too_long))
    assert "не должно быть длиннее" in msg.answer.call_args.args[0]
    assert await db.get_user_settings("999") is None


async def test_forward_to_vk_routes_through_router(temp_db):
    await verify_user()
    await db.create_bridge(555, tg_chat_id=100)

    calls = []

    class FakeRouter:
        async def is_sender_in_conversation(self, vk_peer_id, vk_user_id):
            return True

        async def handle_tg_message(self, bridge, message, text_override=None):
            calls.append((bridge["vk_peer_id"], text_override))

    th.set_message_router(FakeRouter())
    try:
        msg = make_message(chat_id=100)
        msg.text = "vk обычное сообщение"
        await th.forward_to_vk(msg)
        assert calls == [(555, "обычное сообщение")]
    finally:
        th.set_message_router(None)


async def test_forward_to_vk_blocked_when_not_conversation_member(temp_db):
    await verify_user()
    await db.create_bridge(555, tg_chat_id=100)

    calls = []

    class FakeRouter:
        async def is_sender_in_conversation(self, vk_peer_id, vk_user_id):
            return False

        async def handle_tg_message(self, bridge, message, text_override=None):
            calls.append(1)

    th.set_message_router(FakeRouter())
    try:
        msg = make_message(chat_id=100)
        msg.text = "vk обычное сообщение"
        await th.forward_to_vk(msg)
        assert not calls
        assert "не участник" in msg.answer.call_args.args[0]
    finally:
        th.set_message_router(None)


async def test_forward_to_vk_skips_unlinked_chat(temp_db):
    calls = []

    class FakeRouter:
        async def handle_tg_message(self, bridge, message, text_override=None):
            calls.append(1)

    th.set_message_router(FakeRouter())
    try:
        msg = make_message(chat_id=999)
        msg.text = "vk сообщение"
        await th.forward_to_vk(msg)
        assert not calls
    finally:
        th.set_message_router(None)


async def test_forward_to_vk_ignores_commands():
    calls = []

    class FakeRouter:
        async def handle_tg_message(self, bridge, message):
            calls.append(1)

    th.set_message_router(FakeRouter())
    try:
        msg = make_message(chat_id=100)
        msg.text = "/unknown_command"
        await th.forward_to_vk(msg)
        assert not calls
    finally:
        th.set_message_router(None)


async def test_forward_edit_to_vk_routes_and_skips_unlinked(temp_db):
    await db.create_bridge(555, tg_chat_id=100)

    calls = []

    class FakeRouter:
        async def handle_tg_edit(self, bridge, message):
            calls.append(bridge["vk_peer_id"])

    th.set_message_router(FakeRouter())
    try:
        msg = make_message(chat_id=100)
        msg.text = "новый текст"
        await th.forward_edit_to_vk(msg)
        assert calls == [555]

        msg2 = make_message(chat_id=42)
        msg2.text = "новый текст"
        await th.forward_edit_to_vk(msg2)
        assert calls == [555]  # не изменилось
    finally:
        th.set_message_router(None)


def test_create_bot_and_dispatcher():
    bot = th.create_bot()
    dp = th.create_dispatcher()
    assert bot.token == "123456:test_token"
    assert dp is not None
