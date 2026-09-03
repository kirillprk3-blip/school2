import database as db


class FakeMember:
    def __init__(self, member_id):
        self.member_id = member_id


class FakeMembersResult:
    def __init__(self, member_ids):
        self.items = [FakeMember(m) for m in member_ids]


class FakeMessagesAPI:
    def __init__(self, member_ids):
        self.member_ids = member_ids
        self.call_count = 0

    async def get_conversation_members(self, peer_id):
        self.call_count += 1
        return FakeMembersResult(self.member_ids)


class FakeBot:
    def __init__(self, member_ids):
        self.api = type("Api", (), {"messages": FakeMessagesAPI(member_ids)})()


async def test_private_dialog_is_always_member_without_api_call(temp_db):
    bot = FakeBot(member_ids=[])
    assert await db.is_conversation_member_cached(bot, vk_peer_id=100, vk_user_id=999) is True
    assert bot.api.messages.call_count == 0


async def test_conversation_member_check_calls_api(temp_db):
    bot = FakeBot(member_ids=[555, 777])
    assert await db.is_conversation_member_cached(bot, vk_peer_id=2_000_000_100, vk_user_id=555) is True
    assert await db.is_conversation_member_cached(bot, vk_peer_id=2_000_000_100, vk_user_id=1) is False
    assert bot.api.messages.call_count == 1  # второй запрос обслужен кэшем, не долетел до API


async def test_conversation_member_check_fails_closed_on_api_error(temp_db):
    class FailingMessagesAPI:
        async def get_conversation_members(self, peer_id):
            raise RuntimeError("VK API down")

    class FailingBot:
        api = type("Api", (), {"messages": FailingMessagesAPI()})()

    result = await db.is_conversation_member_cached(FailingBot(), vk_peer_id=2_000_000_200, vk_user_id=1)
    assert result is False
