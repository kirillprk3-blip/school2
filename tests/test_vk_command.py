import database as db
import tg_handlers as th
from conftest import make_message, verify_user


class FakeRouter:
    def __init__(self):
        self.calls = []

    async def is_sender_in_conversation(self, vk_peer_id, vk_user_id):
        return True

    async def handle_tg_message(self, bridge, message, text_override=None):
        self.calls.append((bridge["vk_peer_id"], text_override))


# --- _strip_vk_trigger ------------------------------------------------------

def test_strip_vk_trigger_variants():
    assert th._strip_vk_trigger(None) is None
    assert th._strip_vk_trigger("просто текст") is None
    assert th._strip_vk_trigger("vkontakte это что-то другое") is None  # не отдельное слово
    assert th._strip_vk_trigger("vka") is None
    assert th._strip_vk_trigger("vk") == ""
    assert th._strip_vk_trigger("vk   ") == ""
    assert th._strip_vk_trigger("vk текст сообщения") == "текст сообщения"
    assert th._strip_vk_trigger("VK текст сообщения") == "текст сообщения"  # регистронезависимо
    assert th._strip_vk_trigger("Vk текст сообщения") == "текст сообщения"
    assert th._strip_vk_trigger("vK текст сообщения") == "текст сообщения"
    assert th._strip_vk_trigger("vk\nПривет всем!\nВторой абзац.\n\n") == "Привет всем!\nВторой абзац."
    assert th._strip_vk_trigger("vk\tтекст с табом") == "текст с табом"


# --- forward_to_vk: текстовые сообщения -----------------------------------

async def test_text_without_vk_prefix_is_ignored(temp_db):
    await db.create_bridge(555, tg_chat_id=100)
    router = FakeRouter()
    th.set_message_router(router)
    try:
        msg = make_message(chat_id=100)
        msg.text = "обычное сообщение без команды"
        await th.forward_to_vk(msg)
        assert router.calls == []
    finally:
        th.set_message_router(None)


async def test_text_looking_like_vk_word_is_ignored(temp_db):
    """Регрессия: "vkontakte"/"vka" не должны триггерить пересылку."""
    await db.create_bridge(555, tg_chat_id=100)
    router = FakeRouter()
    th.set_message_router(router)
    try:
        msg = make_message(chat_id=100)
        msg.text = "vkontakte вроде бы заблокирован"
        await th.forward_to_vk(msg)
        assert router.calls == []
    finally:
        th.set_message_router(None)


async def test_text_with_vk_prefix_is_forwarded_stripped(temp_db):
    await verify_user()
    await db.create_bridge(555, tg_chat_id=100)
    router = FakeRouter()
    th.set_message_router(router)
    try:
        msg = make_message(chat_id=100)
        msg.text = "vk\nПривет из группы"
        await th.forward_to_vk(msg)
        assert router.calls == [(555, "Привет из группы")]
    finally:
        th.set_message_router(None)


async def test_text_with_uppercase_vk_prefix_is_forwarded(temp_db):
    await verify_user()
    await db.create_bridge(555, tg_chat_id=100)
    router = FakeRouter()
    th.set_message_router(router)
    try:
        msg = make_message(chat_id=100)
        msg.text = "VK привет всем"
        await th.forward_to_vk(msg)
        assert router.calls == [(555, "привет всем")]
    finally:
        th.set_message_router(None)


async def test_empty_vk_trigger_without_media_shows_usage(temp_db):
    await db.create_bridge(555, tg_chat_id=100)
    router = FakeRouter()
    th.set_message_router(router)
    try:
        msg = make_message(chat_id=100)
        msg.text = "vk"
        await th.forward_to_vk(msg)
        assert router.calls == []
        assert "Использование: vk" in msg.answer.call_args.args[0]
    finally:
        th.set_message_router(None)


# --- forward_to_vk: медиа --------------------------------------------------

async def test_media_without_caption_is_ignored(temp_db):
    await db.create_bridge(555, tg_chat_id=100)
    router = FakeRouter()
    th.set_message_router(router)
    try:
        msg = make_message(chat_id=100)
        msg.text = None
        msg.photo = ["fake_photo_size"]
        msg.caption = None
        await th.forward_to_vk(msg)
        assert router.calls == []
    finally:
        th.set_message_router(None)


async def test_media_with_vk_caption_is_forwarded(temp_db):
    await verify_user()
    await db.create_bridge(555, tg_chat_id=100)
    router = FakeRouter()
    th.set_message_router(router)
    try:
        msg = make_message(chat_id=100)
        msg.text = None
        msg.photo = ["fake_photo_size"]
        msg.caption = "vk подпись к фото"
        await th.forward_to_vk(msg)
        assert router.calls == [(555, "подпись к фото")]
    finally:
        th.set_message_router(None)


async def test_media_with_bare_vk_caption_is_forwarded_without_text(temp_db):
    """vk без текста, но с медиа - это не ошибка, пересылаем без подписи."""
    await verify_user()
    await db.create_bridge(555, tg_chat_id=100)
    router = FakeRouter()
    th.set_message_router(router)
    try:
        msg = make_message(chat_id=100)
        msg.text = None
        msg.voice = "fake_voice"
        msg.caption = "vk"
        await th.forward_to_vk(msg)
        assert router.calls == [(555, "")]
        msg.answer.assert_not_called()
    finally:
        th.set_message_router(None)


async def test_media_with_non_vk_caption_is_ignored(temp_db):
    await db.create_bridge(555, tg_chat_id=100)
    router = FakeRouter()
    th.set_message_router(router)
    try:
        msg = make_message(chat_id=100)
        msg.text = None
        msg.video = "fake_video"
        msg.caption = "просто подпись"
        await th.forward_to_vk(msg)
        assert router.calls == []
    finally:
        th.set_message_router(None)
