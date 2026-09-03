import asyncio

import database as db


async def test_bridge_crud_roundtrip(temp_db):
    await db.create_bridge(100, link_code="ABC123")
    bridge = await db.get_bridge_by_vk_peer(100)
    assert bridge == {"vk_peer_id": 100, "tg_chat_id": None, "link_code": "ABC123"}

    by_code = await db.get_bridge_by_link_code("ABC123")
    assert by_code == bridge

    await db.complete_bridge_link(100, 555)
    bridge = await db.get_bridge_by_vk_peer(100)
    assert bridge == {"vk_peer_id": 100, "tg_chat_id": 555, "link_code": None}

    by_chat = await db.get_bridge_by_tg_chat(555)
    assert by_chat == bridge

    await db.delete_bridge(100)
    assert await db.get_bridge_by_vk_peer(100) is None


async def test_complete_bridge_link_rejects_chat_already_bound_elsewhere(temp_db):
    """Регрессия: раньше повторная привязка к уже занятому tg_chat_id падала
    непойманным sqlite3.IntegrityError прямо в хэндлере (частичный UNIQUE-индекс
    bridges.tg_chat_id) - теперь complete_bridge_link просто возвращает False."""
    await db.create_bridge(100, tg_chat_id=555)
    await db.create_bridge(200, link_code="NEW123")

    ok = await db.complete_bridge_link(200, 555)

    assert ok is False
    bridge_200 = await db.get_bridge_by_vk_peer(200)
    assert bridge_200 == {"vk_peer_id": 200, "tg_chat_id": None, "link_code": "NEW123"}
    bridge_100 = await db.get_bridge_by_vk_peer(100)
    assert bridge_100["tg_chat_id"] == 555  # исходный мост не тронут


async def test_complete_bridge_link_returns_true_on_success(temp_db):
    await db.create_bridge(100, link_code="ABC123")
    assert await db.complete_bridge_link(100, 555) is True


async def test_delete_bridge_also_cleans_up_messages_map(temp_db):
    """Регрессия: без очистки messages_map повторная привязка того же vk_peer_id
    к другому tg_chat_id могла бы вернуть в get_tg_message() строку со старым,
    уже неактуальным чатом, и reply/edit ушли бы не туда."""
    await db.create_bridge(100, tg_chat_id=555)
    await db.add_message_map(100, 1, 555, 900)
    await db.add_message_map(100, 2, 555, 901)

    await db.delete_bridge(100)

    assert await db.get_tg_message(100, 1) is None
    assert await db.get_tg_message(100, 2) is None
    assert await db.get_vk_message(555, 900) is None

    # ту же беседу привязали заново к другому чату - старых записей не осталось,
    # чтобы их спутать с новыми
    await db.create_bridge(100, tg_chat_id=777)
    await db.add_message_map(100, 3, 777, 902)
    assert await db.get_tg_message(100, 3) == {"tg_chat_id": 777, "tg_msg_id": 902}


async def test_messages_map_roundtrip(temp_db):
    await db.add_message_map(100, 1, 555, 900)
    assert await db.get_tg_message(100, 1) == {"tg_chat_id": 555, "tg_msg_id": 900}
    assert await db.get_vk_message(555, 900) == {"vk_peer_id": 100, "vk_msg_id": 1}
    assert await db.get_tg_message(100, 999) is None
    assert await db.get_vk_message(555, 999) is None


async def test_user_settings_roundtrip(temp_db):
    assert await db.get_user_settings("999") is None
    await db.set_user_custom_name("999", "Кирилл")
    assert await db.get_user_settings("999") == {"user_id": "999", "custom_name": "Кирилл"}
    await db.set_user_custom_name("999", "Новое имя")
    assert (await db.get_user_settings("999"))["custom_name"] == "Новое имя"


async def test_user_nickname_roundtrip(temp_db):
    assert await db.get_user_nickname(999) is None
    await db.save_user_nickname(999, "Кирилл")
    assert await db.get_user_nickname(999) == "Кирилл"
    await db.save_user_nickname(999, "Новый ник")
    assert await db.get_user_nickname(999) == "Новый ник"
    # разные пользователи не пересекаются
    assert await db.get_user_nickname(1000) is None


async def test_concurrent_writes_do_not_deadlock(temp_db):
    """Регрессия аудита: без busy_timeout параллельные записи могли падать с
    'database is locked'. Гоняем много одновременных записей в обе таблицы."""
    tasks = [db.add_message_map(1, i, 2, 1000 + i) for i in range(25)]
    tasks += [db.set_user_custom_name(str(i), f"User{i}") for i in range(25)]
    await asyncio.gather(*tasks)

    for i in range(25):
        assert await db.get_tg_message(1, i) == {"tg_chat_id": 2, "tg_msg_id": 1000 + i}
        assert (await db.get_user_settings(str(i)))["custom_name"] == f"User{i}"
