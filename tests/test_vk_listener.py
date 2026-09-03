from vkbottle.bot import Bot as VkBot

import database as db
import vk_listener as vl


async def make_vk_bot():
    bot = VkBot(token="test_vk_token")
    return bot


def make_new_message_update(peer_id=100, text="", out=False, from_id=None):
    return {
        "type": "message_new",
        "object": {"message": {
            "id": 1, "date": 0, "from_id": from_id if from_id is not None else peer_id, "peer_id": peer_id,
            "out": out, "text": text, "conversation_message_id": 1,
            "version": 1, "random_id": 0, "attachments": [], "important": False,
            "is_hidden": False, "fwd_messages": [],
        }},
        "group_id": 1,
    }


def make_edit_update(peer_id=100, msg_id=10, text="", out=False):
    return {
        "type": "message_edit",
        "object": {"message": {
            "id": msg_id, "date": 0, "from_id": peer_id, "peer_id": peer_id,
            "out": out, "text": text, "conversation_message_id": 1,
            "version": 1, "random_id": 0, "attachments": [], "important": False,
            "is_hidden": False,
        }},
        "group_id": 1,
    }


async def test_new_peer_creates_bridge_and_sends_link(temp_db):
    bot = await make_vk_bot()
    sent = []

    async def fake_request(method, params=None, **kwargs):
        sent.append({"method": method, **(params or {})})
        return {"response": 1}

    bot.api.request = fake_request
    vl.VKListener(bot=bot, tg_bot_username="test_bot")

    await bot.router.route(make_new_message_update(peer_id=100, text="привет"), bot.api)

    bridge = await db.get_bridge_by_vk_peer(100)
    assert bridge is not None and bridge["link_code"] is not None and bridge["tg_chat_id"] is None
    assert len(sent) == 1
    assert "https://t.me/test_bot?startgroup=" in sent[0]["message"]

    await bot.api.http_client.close()


async def test_repeat_message_does_not_spam_link(temp_db):
    bot = await make_vk_bot()
    sent = []

    async def fake_request(method, params=None, **kwargs):
        sent.append(1)
        return {"response": 1}

    bot.api.request = fake_request
    vl.VKListener(bot=bot, tg_bot_username="test_bot")

    await bot.router.route(make_new_message_update(peer_id=100, text="привет"), bot.api)
    assert len(sent) == 1

    await bot.router.route(make_new_message_update(peer_id=100, text="ещё сообщение"), bot.api)
    assert len(sent) == 1  # не выросло

    await bot.router.route(make_new_message_update(peer_id=100, text="/link"), bot.api)
    assert len(sent) == 2  # явная команда шлёт ссылку снова

    await bot.api.http_client.close()


async def test_outgoing_message_ignored(temp_db):
    bot = await make_vk_bot()
    sent = []

    async def fake_request(method, params=None, **kwargs):
        sent.append(1)
        return {"response": 1}

    bot.api.request = fake_request
    vl.VKListener(bot=bot, tg_bot_username="test_bot")

    await bot.router.route(make_new_message_update(peer_id=100, text="эхо", out=True), bot.api)
    assert not sent
    assert await db.get_bridge_by_vk_peer(100) is None

    await bot.api.http_client.close()


async def test_active_bridge_invokes_callback(temp_db):
    bot = await make_vk_bot()
    calls = []

    async def on_bridged(bridge, message):
        calls.append(bridge["vk_peer_id"])

    await db.create_bridge(100, tg_chat_id=555)
    vl.VKListener(bot=bot, tg_bot_username="test_bot", on_bridged_message=on_bridged)

    await bot.router.route(make_new_message_update(peer_id=100, text="привет"), bot.api)
    assert calls == [100]

    await bot.api.http_client.close()


async def test_message_edit_routing(temp_db):
    bot = await make_vk_bot()
    calls = []

    async def on_edit(bridge, message):
        calls.append((bridge["vk_peer_id"], message.text))

    vl.VKListener(bot=bot, tg_bot_username="test_bot", on_edit_message=on_edit)

    # 1) нет моста вообще -> колбэк не вызывается
    await bot.router.route(make_edit_update(text="правка"), bot.api)
    assert not calls

    # 2) мост есть, но не активен -> не вызывается
    await db.create_bridge(100, link_code="ABC")
    await bot.router.route(make_edit_update(text="правка"), bot.api)
    assert not calls

    # 3) мост активен -> вызывается
    await db.complete_bridge_link(100, 555)
    await bot.router.route(make_edit_update(text="правка"), bot.api)
    assert calls == [(100, "правка")]

    # 4) исходящее -> игнор
    await bot.router.route(make_edit_update(text="ещё", out=True), bot.api)
    assert calls == [(100, "правка")]

    await bot.api.http_client.close()


def test_generate_link_code_format():
    code = vl.generate_link_code()
    assert len(code) == 6
    assert code.isalnum() and code.isupper() or code.isdigit()


# --- верификация через личное сообщение (/link_vk) --------------------------

async def test_private_message_with_valid_code_confirms_verification(temp_db):
    bot = await make_vk_bot()
    sent = []
    verified_calls = []

    async def fake_request(method, params=None, **kwargs):
        if method == "users.get":
            return {"response": [{"id": 777, "first_name": "Иван", "last_name": "Иванов"}]}
        if method == "messages.send":
            sent.append(params or {})
        return {"response": 1}

    bot.api.request = fake_request

    async def on_verified(tg_user_id, vk_full_name, vk_profile_url):
        verified_calls.append((tg_user_id, vk_full_name, vk_profile_url))

    vl.VKListener(bot=bot, tg_bot_username="test_bot", on_vk_verified=on_verified)

    code = await db.create_vk_verification_code(42)

    await bot.router.route(
        make_new_message_update(peer_id=777, from_id=777, text=code), bot.api,
    )

    assert verified_calls == [(42, "Иван Иванов", "vk.com/id777")]
    assert sent == [{"peer_id": 777, "message": "Готово, профиль подтверждён.", "random_id": sent[0]["random_id"]}]

    verification = await db.get_vk_verification(42)
    assert verification["vk_user_id"] == 777

    # личное сообщение с кодом не должно было создать мусорную запись в bridges
    assert await db.get_bridge_by_vk_peer(777) is None

    await bot.api.http_client.close()


async def test_private_message_with_unknown_code_is_silently_ignored(temp_db):
    bot = await make_vk_bot()
    sent = []

    async def fake_request(method, params=None, **kwargs):
        sent.append(1)
        return {"response": 1}

    bot.api.request = fake_request
    vl.VKListener(bot=bot, tg_bot_username="test_bot")

    await bot.router.route(
        make_new_message_update(peer_id=777, from_id=777, text="VZZZZZZZZ"), bot.api,
    )

    assert not sent
    assert await db.get_vk_verification(42) is None

    await bot.api.http_client.close()


async def test_code_like_text_inside_beседа_is_not_treated_as_verification(temp_db):
    """peer_id != from_id - это беседа, а не личное сообщение сообществу:
    даже если текст случайно похож на код верификации, обычная логика
    создания моста должна отработать как всегда."""
    bot = await make_vk_bot()
    sent = []

    async def fake_request(method, params=None, **kwargs):
        sent.append(params or {})
        return {"response": 1}

    bot.api.request = fake_request
    vl.VKListener(bot=bot, tg_bot_username="test_bot")

    code = await db.create_vk_verification_code(42)
    await bot.router.route(
        make_new_message_update(peer_id=2_000_000_100, from_id=555, text=code), bot.api,
    )

    # это должно уйти в обычную ветку "новый мост" (беседа), а не в верификацию
    bridge = await db.get_bridge_by_vk_peer(2_000_000_100)
    assert bridge is not None and bridge["link_code"] is not None
    assert await db.get_vk_verification(42) is None

    await bot.api.http_client.close()
