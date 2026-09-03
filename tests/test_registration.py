import ast
from pathlib import Path

from aiogram.filters import CommandObject

import database as db
import tg_handlers as th
from conftest import make_message, make_state, verify_user


# --- Полный сценарий регистрации -------------------------------------------

async def test_start_without_code_sets_waiting_state_and_no_pending_code(temp_db):
    await verify_user()
    msg = make_message(user_id=999)
    state = make_state(user_id=999)

    await th.cmd_start(msg, CommandObject(prefix="/", command="start", args=None), state)

    assert await state.get_state() == th.RegistrationState.waiting_for_nickname.state
    data = await state.get_data()
    assert "pending_link_code" not in data


async def test_start_with_code_stores_code_in_fsm_data(temp_db):
    await verify_user()
    msg = make_message(user_id=999)
    state = make_state(user_id=999)

    await th.cmd_start(msg, CommandObject(prefix="/", command="start", args="ABC123"), state)

    assert await state.get_state() == th.RegistrationState.waiting_for_nickname.state
    data = await state.get_data()
    assert data["pending_link_code"] == "ABC123"


async def test_process_nickname_too_short_reprompts_and_keeps_state(temp_db):
    msg = make_message(user_id=999)
    msg.text = "Я"
    state = make_state(user_id=999)
    await state.set_state(th.RegistrationState.waiting_for_nickname)

    await th.process_nickname(msg, state)

    assert "от 2 до 32" in msg.answer.call_args.args[0]
    assert await state.get_state() == th.RegistrationState.waiting_for_nickname.state
    assert await db.get_user_nickname(999) is None


async def test_process_nickname_too_long_reprompts(temp_db):
    msg = make_message(user_id=999)
    msg.text = "Ф" * 33
    state = make_state(user_id=999)
    await state.set_state(th.RegistrationState.waiting_for_nickname)

    await th.process_nickname(msg, state)

    assert "от 2 до 32" in msg.answer.call_args.args[0]
    assert await db.get_user_nickname(999) is None


async def test_process_nickname_valid_saves_and_clears_state(temp_db):
    msg = make_message(user_id=999)
    msg.text = "Кирилл"
    state = make_state(user_id=999)
    await state.set_state(th.RegistrationState.waiting_for_nickname)

    await th.process_nickname(msg, state)

    assert await db.get_user_nickname(999) == "Кирилл"
    assert await state.get_state() is None

    # ответ: подтверждение + главное меню с инструкцией и кнопкой (два вызова answer)
    assert msg.answer.call_count == 2
    confirm_text = msg.answer.call_args_list[0].args[0]
    menu_text = msg.answer.call_args_list[1].args[0]
    assert "Кирилл" in confirm_text
    assert "Шаг 1" in menu_text and "Шаг 4" in menu_text
    assert msg.answer.call_args_list[1].kwargs["reply_markup"] is not None


async def test_process_nickname_trims_whitespace(temp_db):
    msg = make_message(user_id=999)
    msg.text = "  Ник  "
    state = make_state(user_id=999)
    await state.set_state(th.RegistrationState.waiting_for_nickname)

    await th.process_nickname(msg, state)

    assert await db.get_user_nickname(999) == "Ник"


async def test_process_nickname_boundary_lengths_accepted(temp_db):
    msg = make_message(user_id=1)
    msg.text = "аб"  # 2 символа, нижняя граница
    state = make_state(user_id=1)
    await state.set_state(th.RegistrationState.waiting_for_nickname)
    await th.process_nickname(msg, state)
    assert await db.get_user_nickname(1) == "аб"

    msg2 = make_message(user_id=2)
    msg2.text = "Ы" * 32  # 32 символа, верхняя граница
    state2 = make_state(user_id=2)
    await state2.set_state(th.RegistrationState.waiting_for_nickname)
    await th.process_nickname(msg2, state2)
    assert await db.get_user_nickname(2) == "Ы" * 32


async def test_process_nickname_with_pending_code_links_bridge(temp_db):
    await db.create_bridge(555, link_code="XYZ789")

    msg = make_message(chat_id=100, user_id=999)
    msg.text = "Кирилл"
    state = make_state(chat_id=100, user_id=999)
    await state.set_state(th.RegistrationState.waiting_for_nickname)
    await state.update_data(pending_link_code="XYZ789")

    await th.process_nickname(msg, state)

    bridge = await db.get_bridge_by_vk_peer(555)
    assert bridge == {"vk_peer_id": 555, "tg_chat_id": 100, "link_code": None}

    menu_text = msg.answer.call_args_list[1].args[0]
    assert "555" in menu_text  # заметка о привязке попала в текст перед инструкцией


async def test_process_nickname_with_stale_pending_code_does_not_crash(temp_db):
    msg = make_message(chat_id=100, user_id=999)
    msg.text = "Кирилл"
    state = make_state(chat_id=100, user_id=999)
    await state.set_state(th.RegistrationState.waiting_for_nickname)
    await state.update_data(pending_link_code="NOSUCHCODE")

    await th.process_nickname(msg, state)  # не должно бросить исключение

    assert await db.get_user_nickname(999) == "Кирилл"
    menu_text = msg.answer.call_args_list[1].args[0]
    assert "недействителен" in menu_text


async def test_full_registration_flow_end_to_end(temp_db):
    """Сквозной сценарий: /start с кодом -> регистрация -> автопривязка."""
    await verify_user(tg_user_id=42, vk_user_id=201)
    await db.create_bridge(777, link_code="FULL01")

    msg1 = make_message(chat_id=200, user_id=42)
    state = make_state(chat_id=200, user_id=42)
    await th.cmd_start(msg1, CommandObject(prefix="/", command="start", args="FULL01"), state)
    assert await state.get_state() == th.RegistrationState.waiting_for_nickname.state

    msg2 = make_message(chat_id=200, user_id=42)
    msg2.text = "Тестовый Ник"
    await th.process_nickname(msg2, state)

    assert await db.get_user_nickname(42) == "Тестовый Ник"
    assert await db.get_bridge_by_vk_peer(777) == {
        "vk_peer_id": 777, "tg_chat_id": 200, "link_code": None,
    }
    assert await state.get_state() is None


async def test_registered_user_start_skips_registration(temp_db):
    await verify_user()
    await db.save_user_nickname(999, "Уже зарегистрирован")
    msg = make_message(user_id=999)
    state = make_state(user_id=999)

    await th.cmd_start(msg, CommandObject(prefix="/", command="start", args=None), state)

    assert await state.get_state() is None
    assert "Уже зарегистрирован" in msg.answer.call_args.args[0]


# --- Кнопка "Связаться с админом" -------------------------------------------

def test_admin_keyboard_has_correct_url_button():
    kb = th._admin_keyboard()
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    assert len(buttons) == 1
    assert buttons[0].text == "Связаться с админом"
    assert buttons[0].url == "https://t.me/s1hopu"


async def test_main_menu_attaches_admin_keyboard(temp_db):
    await verify_user()
    await db.save_user_nickname(999, "Кирилл")
    msg = make_message(user_id=999)
    state = make_state(user_id=999)

    await th.cmd_start(msg, CommandObject(prefix="/", command="start", args=None), state)

    kb = msg.answer.call_args.kwargs["reply_markup"]
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    assert buttons[0].url == "https://t.me/s1hopu"


# --- Правило "никаких длинных тире" -----------------------------------------

def test_no_em_dash_in_instructions_text():
    assert "—" not in th.INSTRUCTIONS_TEXT


def test_no_em_dash_anywhere_in_tg_handlers_source():
    """Сканирует все строковые литералы в tg_handlers.py на предмет длинного
    тире '—' (U+2014) - буквально требование ТЗ Этапа 6."""
    tg_handlers_path = Path(__file__).resolve().parent.parent / "tg_handlers.py"
    source = tg_handlers_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    offending = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "—" in node.value
    ]
    assert offending == [], f"Найдены строки с длинным тире: {offending}"
