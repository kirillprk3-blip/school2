from unittest.mock import AsyncMock, MagicMock

import database as db
import tg_handlers as th
from conftest import make_message


def _member_regular():
    m = AsyncMock()
    m.status = "member"
    return m


# --- /whois -------------------------------------------------------------

async def test_whois_requires_admin(temp_db):
    await db.create_bridge(555, tg_chat_id=100)
    msg = make_message(chat_id=100, chat_type="group")
    msg.bot.get_chat_member.return_value = _member_regular()
    msg.reply_to_message = MagicMock()
    msg.reply_to_message.message_id = 900

    await th.cmd_whois(msg)

    assert "администратор" in msg.answer.call_args.args[0]


async def test_whois_requires_reply(temp_db):
    await db.create_bridge(555, tg_chat_id=100)
    msg = make_message(chat_id=100, chat_type="private")
    msg.reply_to_message = None

    await th.cmd_whois(msg)

    assert "Ответь" in msg.answer.call_args.args[0]


async def test_whois_requires_bridge(temp_db):
    msg = make_message(chat_id=999, chat_type="private")
    msg.reply_to_message = MagicMock()
    msg.reply_to_message.message_id = 1

    await th.cmd_whois(msg)

    assert "не привязан" in msg.answer.call_args.args[0]


async def test_whois_resolves_vk_sender(temp_db):
    await db.create_bridge(555, tg_chat_id=100)
    await db.add_message_history(
        555, "vk_to_tg", sender_tg_user_id=None, sender_vk_user_id=777,
        display_name="Иван Иванов", text="привет", has_media=False,
        tg_msg_id=900, vk_msg_id=1,
    )
    msg = make_message(chat_id=100, chat_type="private")
    msg.reply_to_message = MagicMock()
    msg.reply_to_message.message_id = 900

    await th.cmd_whois(msg)

    text = msg.answer.call_args.args[0]
    assert "пользователь VK" in text
    assert "vk.com/id777" in text


async def test_whois_resolves_verified_tg_sender(temp_db):
    await db.create_bridge(555, tg_chat_id=100)
    await db.confirm_vk_verification(42, vk_user_id=1, vk_profile_url="vk.com/id1", vk_full_name="Верифицированный")
    await db.add_message_history(
        555, "tg_to_vk", sender_tg_user_id=42, sender_vk_user_id=None,
        display_name="Кирилл", text="ответ", has_media=False,
        tg_msg_id=5, vk_msg_id=700,
    )
    msg = make_message(chat_id=100, chat_type="private")
    msg.reply_to_message = MagicMock()
    msg.reply_to_message.message_id = 5

    await th.cmd_whois(msg)

    text = msg.answer.call_args.args[0]
    assert "Telegram" in text
    assert "vk.com/id1" in text


async def test_whois_unverified_tg_sender_reports_no_vk(temp_db):
    await db.create_bridge(555, tg_chat_id=100)
    await db.add_message_history(
        555, "tg_to_vk", sender_tg_user_id=42, sender_vk_user_id=None,
        display_name="Кирилл", text="ответ", has_media=False,
        tg_msg_id=5, vk_msg_id=700,
    )
    msg = make_message(chat_id=100, chat_type="private")
    msg.reply_to_message = MagicMock()
    msg.reply_to_message.message_id = 5

    await th.cmd_whois(msg)

    text = msg.answer.call_args.args[0]
    assert "не подтверждена" in text


async def test_whois_unknown_message_reports_not_found(temp_db):
    await db.create_bridge(555, tg_chat_id=100)
    msg = make_message(chat_id=100, chat_type="private")
    msg.reply_to_message = MagicMock()
    msg.reply_to_message.message_id = 999

    await th.cmd_whois(msg)

    assert "Не нашёл" in msg.answer.call_args.args[0]


# --- /report --------------------------------------------------------------

async def test_report_requires_admin(temp_db):
    msg = make_message(chat_id=100, chat_type="group")
    msg.bot.get_chat_member.return_value = _member_regular()

    await th.cmd_report(msg)

    assert "администратор" in msg.answer.call_args.args[0]


async def test_report_requires_bridge(temp_db):
    msg = make_message(chat_id=999, chat_type="private")

    await th.cmd_report(msg)

    assert "не привязан" in msg.answer.call_args.args[0]


async def test_report_empty_activity(temp_db):
    await db.create_bridge(555, tg_chat_id=100)
    msg = make_message(chat_id=100, chat_type="private")

    await th.cmd_report(msg)

    assert "Пока нет" in msg.answer.call_args.args[0]


async def test_report_sends_summary_and_csv(temp_db):
    await db.create_bridge(555, tg_chat_id=100)
    await db.confirm_vk_verification(999, vk_user_id=1, vk_profile_url="vk.com/id1", vk_full_name="Тест")
    await db.bump_user_activity(999, 555)
    await db.bump_user_activity(999, 555)

    msg = make_message(chat_id=100, chat_type="private")
    msg.answer_document = AsyncMock()

    await th.cmd_report(msg)

    summary = msg.answer.call_args.args[0]
    assert "Участников: 1" in summary
    assert "Тест" in summary

    msg.answer_document.assert_called_once()
    document = msg.answer_document.call_args.args[0]
    csv_text = document.data.decode("utf-8-sig")
    assert "Тест" in csv_text
    assert "vk.com/id1" in csv_text


def test_csv_safe_neutralizes_formula_injection():
    assert th._csv_safe("=cmd()") == "'=cmd()"
    assert th._csv_safe("Обычное имя") == "Обычное имя"
