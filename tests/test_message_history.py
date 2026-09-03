import database as db


# --- add_message_history / get_message_history_by_tg_msg --------------------

async def test_add_message_history_vk_to_tg_and_query(temp_db):
    await db.add_message_history(
        100, "vk_to_tg", sender_tg_user_id=None, sender_vk_user_id=555,
        display_name="Иван Иванов", text="привет", has_media=False,
        tg_msg_id=900, vk_msg_id=1,
    )
    record = await db.get_message_history_by_tg_msg(100, 900)
    assert record["direction"] == "vk_to_tg"
    assert record["sender_vk_user_id"] == 555
    assert record["sender_tg_user_id"] is None
    assert record["display_name"] == "Иван Иванов"
    assert record["text"] == "привет"


async def test_add_message_history_tg_to_vk_and_query(temp_db):
    await db.add_message_history(
        100, "tg_to_vk", sender_tg_user_id=999, sender_vk_user_id=None,
        display_name="Кирилл", text="ответ", has_media=False,
        tg_msg_id=5, vk_msg_id=700,
    )
    record = await db.get_message_history_by_tg_msg(100, 5)
    assert record["direction"] == "tg_to_vk"
    assert record["sender_tg_user_id"] == 999
    assert record["sender_vk_user_id"] is None


async def test_get_message_history_by_tg_msg_returns_none_when_missing(temp_db):
    assert await db.get_message_history_by_tg_msg(100, 999) is None


# --- bump_user_activity / get_activity_report --------------------------------

async def test_bump_user_activity_increments(temp_db):
    await db.bump_user_activity(999, 100)
    await db.bump_user_activity(999, 100)
    await db.bump_user_activity(999, 100)

    rows = await db.get_activity_report(100)
    assert len(rows) == 1
    assert rows[0]["message_count"] == 3


async def test_bump_user_activity_separates_by_peer(temp_db):
    await db.bump_user_activity(999, 100)
    await db.bump_user_activity(999, 200)

    rows100 = await db.get_activity_report(100)
    rows200 = await db.get_activity_report(200)
    assert rows100[0]["message_count"] == 1
    assert rows200[0]["message_count"] == 1


async def test_get_activity_report_uses_current_name_priority(temp_db):
    """Отчёт всегда показывает ТЕКУЩЕЕ имя (не снепшот из message_history) -
    тот же приоритет, что и router._display_name."""
    await db.bump_user_activity(999, 100)

    rows = await db.get_activity_report(100)
    assert rows[0]["display_name"] == "999"  # ничего не задано - tg_user_id как строка

    await db.confirm_vk_verification(999, vk_user_id=1, vk_profile_url="vk.com/id1", vk_full_name="VK Имя")
    rows = await db.get_activity_report(100)
    assert rows[0]["display_name"] == "VK Имя"

    await db.set_user_custom_name("999", "Кастом")
    rows = await db.get_activity_report(100)
    assert rows[0]["display_name"] == "Кастом"  # custom_name побеждает даже подтверждённый VK-профиль


async def test_get_activity_report_sorted_by_message_count_desc(temp_db):
    await db.bump_user_activity(1, 100)
    for _ in range(3):
        await db.bump_user_activity(2, 100)
    for _ in range(2):
        await db.bump_user_activity(3, 100)

    rows = await db.get_activity_report(100)
    assert [r["tg_user_id"] for r in rows] == [2, 3, 1]
