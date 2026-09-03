from unittest.mock import AsyncMock

import pytest
from vkbottle.bot import Bot as VkBot
from vkbottle.exception_factory import VKAPIError

import vk_sender as vs


@pytest.fixture
async def vk_bot():
    bot = VkBot(token="test_vk_token")
    yield bot
    await bot.api.http_client.close()


async def test_send_text_happy_path(vk_bot):
    calls = []

    async def fake_request(method, params=None, **kwargs):
        calls.append({"method": method, **(params or {})})
        return {"response": 42}

    vk_bot.api.request = fake_request
    sender = vs.VKSender(vk_bot)

    msg_id = await sender.send_text(peer_id=100, text="привет")
    assert msg_id == 42
    assert calls[-1]["method"] == "messages.send"
    assert calls[-1]["peer_id"] == 100


async def test_send_text_error_returns_none(vk_bot):
    async def failing_request(method, params=None, **kwargs):
        raise RuntimeError("network down")

    vk_bot.api.request = failing_request
    sender = vs.VKSender(vk_bot)

    assert await sender.send_text(peer_id=100, text="привет") is None


async def test_send_text_truncates_to_vk_limit(vk_bot):
    calls = []

    async def fake_request(method, params=None, **kwargs):
        calls.append(params or {})
        return {"response": 1}

    vk_bot.api.request = fake_request
    sender = vs.VKSender(vk_bot)

    long_text = "A" * 5000
    await sender.send_text(peer_id=100, text=long_text)
    assert len(calls[-1]["message"]) == vs.VK_TEXT_LIMIT
    assert calls[-1]["message"].endswith("…")


async def test_send_text_retries_once_on_rate_limit(vk_bot):
    attempts = []

    async def flaky_request(method, params=None, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise VKAPIError[6](error_msg="Too many requests per second", request_params=[])
        return {"response": 99}

    vk_bot.api.request = flaky_request
    sender = vs.VKSender(vk_bot)

    msg_id = await sender.send_text(peer_id=100, text="привет")
    assert msg_id == 99
    assert len(attempts) == 2


async def test_send_text_falls_back_without_reply_to_on_stale_id(vk_bot):
    calls = []

    async def fake_request(method, params=None, **kwargs):
        calls.append(dict(params or {}))
        if params.get("reply_to") is not None:
            raise RuntimeError("reply message not found")
        return {"response": 55}

    vk_bot.api.request = fake_request
    sender = vs.VKSender(vk_bot)

    msg_id = await sender.send_text(peer_id=100, text="ответ", reply_to=999999)
    assert msg_id == 55
    assert len(calls) == 2
    assert calls[0]["reply_to"] == 999999
    assert "reply_to" not in calls[1]


async def test_send_attachment_and_send_file(vk_bot):
    calls = []

    async def fake_request(method, params=None, **kwargs):
        calls.append({"method": method, **(params or {})})
        return {"response": 7}

    vk_bot.api.request = fake_request
    sender = vs.VKSender(vk_bot)

    msg_id = await sender.send_attachment(peer_id=100, attachment="photo1_1", caption="подпись")
    assert msg_id == 7
    assert calls[-1]["attachment"] == "photo1_1"


async def test_edit_and_delete(vk_bot):
    calls = []

    async def fake_request(method, params=None, **kwargs):
        calls.append({"method": method, **(params or {})})
        if method == "messages.delete":
            return {"response": [{"peer_id": 100, "message_id": 1, "response": True}]}
        return {"response": True}

    vk_bot.api.request = fake_request
    sender = vs.VKSender(vk_bot)

    assert await sender.edit_text(peer_id=100, message_id=1, text="новый текст") is True
    assert await sender.delete_message(peer_id=100, message_id=1) is True
