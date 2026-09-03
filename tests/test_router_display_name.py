from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot as TgBot
from aiogram.types import Chat, Message as TgMessage, User as TgUser
from vkbottle.bot import Bot as VkBot

import database as db
import router as r


@pytest.fixture
async def tg_bot():
    bot = TgBot(token="123456:test_token")
    yield bot
    await bot.session.close()


@pytest.fixture
async def vk_bot():
    async def fake_request(method, params=None, **kwargs):
        return {"response": 1}

    bot = VkBot(token="test_vk_token")
    bot.api.request = fake_request
    yield bot
    await bot.api.http_client.close()


@pytest.fixture
def mr(tg_bot, vk_bot):
    return r.MessageRouter(tg_bot=tg_bot, vk_bot=vk_bot)


def make_tg_msg(message_id=1, chat_id=555, user_id=999, text="", username=None, **extra):
    return TgMessage.model_validate(dict(
        message_id=message_id, date=0, chat=Chat(id=chat_id, type="private"),
        from_user=TgUser(id=user_id, is_bot=False, first_name="Кирилл", username=username),
        text=text, **extra,
    ))


async def make_bridge(vk_peer_id=100, tg_chat_id=555):
    await db.create_bridge(vk_peer_id, tg_chat_id=tg_chat_id)
    return await db.get_bridge_by_vk_peer(vk_peer_id)


# --- _display_name напрямую ---------------------------------------------------

async def test_display_name_tg_with_username(temp_db):
    text = await r._display_name("tg", 999, fallback="Кирилл", username="kir")
    assert text == "Кирилл (@kir)"


async def test_display_name_tg_without_username(temp_db):
    text = await r._display_name("tg", 999, fallback="Кирилл", username=None)
    assert text == "Кирилл"


async def test_display_name_vk_ignores_username(temp_db):
    text = await r._display_name("vk", 200, fallback="Иван Иванов", username="ignored")
    assert text == "Иван Иванов"


async def test_display_name_priority_custom_name_over_nickname_over_fallback(temp_db):
    # только fallback
    assert await r._display_name("tg", 1, fallback="Из TG") == "Из TG"

    # nickname есть, custom_name нет -> nickname
    await db.save_user_nickname(1, "Ник")
    assert await r._display_name("tg", 1, fallback="Из TG") == "Ник"

    # оба есть -> custom_name побеждает
    await db.set_user_custom_name("1", "Кастом")
    assert await r._display_name("tg", 1, fallback="Из TG") == "Кастом"


# --- handle_tg_message: реальная шапка сообщения ------------------------------

async def test_handle_tg_message_header_includes_username(temp_db, mr):
    bridge = await make_bridge()
    with patch.object(mr.vk_sender, "send_text", new=AsyncMock(return_value=700)) as st:
        await mr.handle_tg_message(bridge, make_tg_msg(text="привет", username="kir"))
        assert st.call_args.args[1].startswith("Кирилл (@kir):")


async def test_handle_tg_message_header_without_username(temp_db, mr):
    bridge = await make_bridge()
    with patch.object(mr.vk_sender, "send_text", new=AsyncMock(return_value=700)) as st:
        await mr.handle_tg_message(bridge, make_tg_msg(text="привет", username=None))
        assert st.call_args.args[1].startswith("Кирилл:")
        assert "@" not in st.call_args.args[1].split(":")[0]


async def test_handle_tg_message_uses_nickname_when_no_custom_name(temp_db, mr):
    await db.save_user_nickname(999, "МойНик")
    bridge = await make_bridge()
    with patch.object(mr.vk_sender, "send_text", new=AsyncMock(return_value=700)) as st:
        await mr.handle_tg_message(bridge, make_tg_msg(text="привет", username="kir"))
        assert st.call_args.args[1].startswith("МойНик (@kir):")
