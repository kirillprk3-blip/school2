from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot as TgBot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import Chat, Message as TgMessage, User as TgUser
from vkbottle.bot import Bot as VkBot, Message as VkMessage

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
        if method == "users.get":
            return {"response": [{"id": 200, "first_name": "Иван", "last_name": "Иванов"}]}
        return {"response": 1}

    bot = VkBot(token="test_vk_token")
    bot.api.request = fake_request
    yield bot
    await bot.api.http_client.close()


@pytest.fixture
def mr(tg_bot, vk_bot):
    return r.MessageRouter(tg_bot=tg_bot, vk_bot=vk_bot)


def make_vk_msg(peer_id=100, from_id=200, text="", attachments=None, reply_message=None):
    d = {
        "id": 1, "date": 0, "from_id": from_id, "peer_id": peer_id,
        "out": False, "text": text, "conversation_message_id": 1,
        "version": 1, "random_id": 0, "attachments": attachments or [],
        "important": False, "is_hidden": False,
    }
    if reply_message:
        d["reply_message"] = reply_message
    return VkMessage.model_validate(d)


def make_tg_msg(message_id=1, chat_id=555, user_id=999, text="", is_bot=False,
                 reply_to_message=None, **extra):
    d = dict(
        message_id=message_id, date=0, chat=Chat(id=chat_id, type="private"),
        from_user=TgUser(id=user_id, is_bot=is_bot, first_name="Кирилл"),
        text=text, **extra,
    )
    if reply_to_message:
        d["reply_to_message"] = reply_to_message
    return TgMessage.model_validate(d)


async def make_bridge(temp_db, vk_peer_id=100, tg_chat_id=555):
    await db.create_bridge(vk_peer_id, tg_chat_id=tg_chat_id)
    return await db.get_bridge_by_vk_peer(vk_peer_id)


# --- VK -> TG -------------------------------------------------------------

async def test_vk_text_forwarded_and_mapped(temp_db, mr, tg_bot):
    bridge = await make_bridge(temp_db)
    with patch.object(tg_bot, "send_message", new=AsyncMock(return_value=type("M", (), {"message_id": 900})())) as sm:
        await mr.handle_vk_message(bridge, make_vk_msg(text="привет"))
        assert sm.call_args.args[1].startswith("Иван Иванов:")
    assert await db.get_tg_message(100, 1) == {"tg_chat_id": 555, "tg_msg_id": 900}


async def test_vk_community_message_ignored(temp_db, mr, tg_bot):
    bridge = await make_bridge(temp_db)
    with patch.object(tg_bot, "send_message", new=AsyncMock()) as sm:
        await mr.handle_vk_message(bridge, make_vk_msg(from_id=-123, text="реклама"))
        assert sm.call_count == 0


async def test_vk_long_text_truncated_for_tg(temp_db, mr, tg_bot):
    bridge = await make_bridge(temp_db)
    long_text = "Б" * 5000
    with patch.object(tg_bot, "send_message", new=AsyncMock(return_value=type("M", (), {"message_id": 901})())) as sm:
        await mr.handle_vk_message(bridge, make_vk_msg(text=long_text))
        sent_text = sm.call_args.args[1]
        assert len(sent_text) <= r.TG_TEXT_LIMIT
        assert sent_text.endswith("…")


async def test_vk_reply_resolves_tg_reply_id(temp_db, mr, tg_bot):
    bridge = await make_bridge(temp_db)
    with patch.object(tg_bot, "send_message", new=AsyncMock(return_value=type("M", (), {"message_id": 900})())):
        await mr.handle_vk_message(bridge, make_vk_msg(text="оригинал"))

    reply_obj = {"id": 1, "date": 0, "from_id": 200, "peer_id": 100, "out": False,
                 "text": "оригинал", "conversation_message_id": 1, "version": 1, "random_id": 0}
    with patch.object(tg_bot, "send_message", new=AsyncMock(return_value=type("M", (), {"message_id": 901})())) as sm:
        await mr.handle_vk_message(bridge, make_vk_msg(text="ответ", reply_message=reply_obj))
        assert sm.call_args.kwargs.get("reply_to_message_id") == 900
        assert sm.call_args.kwargs.get("allow_sending_without_reply") is True


async def test_vk_sticker_forwarded_as_photo(temp_db, mr, tg_bot):
    bridge = await make_bridge(temp_db)
    sticker_attachment = {
        "type": "sticker",
        "sticker": {
            "inner_type": "sticker", "sticker_id": 1, "product_id": 1,
            "images": [{"url": "http://x/sticker.png", "width": 100, "height": 100}],
            "images_with_background": [], "is_allowed": True,
        },
    }
    with patch("router._download_bytes", new=AsyncMock(return_value=b"PNGDATA")):
        with patch.object(tg_bot, "send_photo", new=AsyncMock(return_value=type("M", (), {"message_id": 902})())) as sp:
            await mr.handle_vk_message(bridge, make_vk_msg(attachments=[sticker_attachment]))
            assert sp.call_count == 1


async def test_vk_unsupported_attachment_falls_back_to_placeholder(temp_db, mr, tg_bot):
    """Регрессия аудита: раньше документ/опрос/etc. без текста терялись молча."""
    bridge = await make_bridge(temp_db)
    doc_attachment = {"type": "doc", "doc": {
        "id": 1, "owner_id": 1, "title": "file.pdf", "size": 100, "ext": "pdf", "date": 0, "type": 8,
    }}
    with patch.object(tg_bot, "send_message", new=AsyncMock(return_value=type("M", (), {"message_id": 903})())) as sm:
        await mr.handle_vk_message(bridge, make_vk_msg(attachments=[doc_attachment]))
        assert sm.call_count == 1
        assert "неподдерживаемого типа" in sm.call_args.args[1]
    assert await db.get_tg_message(100, 1) == {"tg_chat_id": 555, "tg_msg_id": 903}


# --- TG -> VK -------------------------------------------------------------

async def test_tg_text_forwarded_and_mapped(temp_db, mr):
    bridge = await make_bridge(temp_db)
    with patch.object(mr.vk_sender, "send_text", new=AsyncMock(return_value=700)) as st:
        await mr.handle_tg_message(bridge, make_tg_msg(text="привет из тг"))
        assert st.call_args.args[1].startswith("Кирилл:")
    assert await db.get_vk_message(555, 1) == {"vk_peer_id": 100, "vk_msg_id": 700}


async def test_tg_bot_message_ignored(temp_db, mr):
    bridge = await make_bridge(temp_db)
    with patch.object(mr.vk_sender, "send_text", new=AsyncMock()) as st:
        await mr.handle_tg_message(bridge, make_tg_msg(text="я бот", is_bot=True))
        assert st.call_count == 0


async def test_tg_unsupported_content_falls_back_to_placeholder(temp_db, mr):
    """Регрессия аудита: TG-стикер/контакт/локация без текста/фото/видео/voice
    раньше терялись молча (ни одна ветка if/elif не матчилась)."""
    bridge = await make_bridge(temp_db)
    with patch.object(mr.vk_sender, "send_text", new=AsyncMock(return_value=701)) as st:
        msg = make_tg_msg(text=None)  # ни text, ни voice/photo/video
        await mr.handle_tg_message(bridge, msg)
        assert st.call_count == 1
        assert "неподдерживаемого типа" in st.call_args.args[1]


async def test_tg_reply_resolves_vk_reply_id(temp_db, mr):
    bridge = await make_bridge(temp_db)
    with patch.object(mr.vk_sender, "send_text", new=AsyncMock(return_value=700)):
        await mr.handle_tg_message(bridge, make_tg_msg(message_id=1, text="оригинал"))

    reply_to_msg = make_tg_msg(message_id=1, text="оригинал")
    with patch.object(mr.vk_sender, "send_text", new=AsyncMock(return_value=701)) as st:
        await mr.handle_tg_message(
            bridge, make_tg_msg(message_id=2, text="ответ", reply_to_message=reply_to_msg),
        )
        assert st.call_args.kwargs.get("reply_to") == 700


# --- Редактирование --------------------------------------------------------

async def test_vk_edit_updates_tg(temp_db, mr, tg_bot):
    bridge = await make_bridge(temp_db)
    await db.add_message_map(100, 10, 555, 900)
    with patch.object(tg_bot, "edit_message_text", new=AsyncMock()) as em:
        await mr.handle_vk_edit(bridge, make_vk_msg(text="исправлено"))
        # id по умолчанию в make_vk_msg = 1, переопределим на 10 для соответствия мапе
    # повторим с явным id
    with patch.object(tg_bot, "edit_message_text", new=AsyncMock()) as em2:
        msg = VkMessage.model_validate({
            "id": 10, "date": 0, "from_id": 200, "peer_id": 100, "out": False,
            "text": "исправлено2", "conversation_message_id": 1, "version": 1,
            "random_id": 0, "attachments": [], "important": False, "is_hidden": False,
        })
        await mr.handle_vk_edit(bridge, msg)
        assert em2.call_args.kwargs["chat_id"] == 555
        assert em2.call_args.kwargs["message_id"] == 900


async def test_tg_edit_updates_vk(temp_db, mr):
    bridge = await make_bridge(temp_db)
    await db.add_message_map(100, 10, 555, 900)
    with patch.object(mr.vk_sender, "edit_text", new=AsyncMock(return_value=True)) as et:
        await mr.handle_tg_edit(bridge, make_tg_msg(message_id=900, text="новый текст"))
        assert et.call_args.args[0] == 100
        assert et.call_args.args[1] == 10


async def test_edit_of_unmapped_message_is_noop(temp_db, mr, tg_bot):
    bridge = await make_bridge(temp_db)
    with patch.object(tg_bot, "edit_message_text", new=AsyncMock()) as em:
        msg = VkMessage.model_validate({
            "id": 999, "date": 0, "from_id": 200, "peer_id": 100, "out": False,
            "text": "нет в мапе", "conversation_message_id": 1, "version": 1,
            "random_id": 0, "attachments": [], "important": False, "is_hidden": False,
        })
        await mr.handle_vk_edit(bridge, msg)
        assert em.call_count == 0


async def test_tg_edit_flood_wait_retried(temp_db, mr):
    """TelegramRetryAfter при tg_bot.edit_message_text должен быть пойман и
    отработан без падения обработчика (retry идёт на стороне aiogram-вызовов
    из router._call_tg; здесь проверяем, что edit VK->TG вообще не падает)."""
    bridge = await make_bridge(temp_db)
    await db.add_message_map(100, 10, 555, 900)

    async def flaky_edit(**kwargs):
        raise RuntimeError("temporary failure")

    with patch.object(mr.tg_bot, "edit_message_text", new=flaky_edit):
        msg = VkMessage.model_validate({
            "id": 10, "date": 0, "from_id": 200, "peer_id": 100, "out": False,
            "text": "правка", "conversation_message_id": 1, "version": 1,
            "random_id": 0, "attachments": [], "important": False, "is_hidden": False,
        })
        # не должно бросить исключение наружу
        await mr.handle_vk_edit(bridge, msg)


# --- Удаление ---------------------------------------------------------------

async def test_delete_helpers(temp_db, mr, tg_bot):
    bridge = await make_bridge(temp_db)
    await db.add_message_map(100, 10, 555, 900)

    with patch.object(tg_bot, "delete_message", new=AsyncMock(return_value=True)) as dm:
        assert await mr.handle_vk_delete(bridge, vk_msg_id=10) is True
        assert dm.call_args.kwargs == {"chat_id": 555, "message_id": 900}

    with patch.object(mr.vk_sender, "delete_message", new=AsyncMock(return_value=True)) as vd:
        assert await mr.handle_tg_delete(bridge, tg_msg_id=900) is True
        assert vd.call_args.args == (100, 10)


async def test_delete_unmapped_returns_false(temp_db, mr, tg_bot):
    bridge = await make_bridge(temp_db)
    assert await mr.handle_vk_delete(bridge, vk_msg_id=999) is False
    assert await mr.handle_tg_delete(bridge, tg_msg_id=999) is False


# --- TG FloodWait retry helper ----------------------------------------------

async def test_call_tg_retries_on_flood_wait():
    attempts = []

    async def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise TelegramRetryAfter(
                method=SendMessage(chat_id=1, text="x"), message="Too Many Requests", retry_after=0,
            )
        return "ok"

    result = await r._call_tg(flaky)
    assert result == "ok"
    assert len(attempts) == 2
