import re
from unittest.mock import patch

import aiosqlite

import database as db

CODE_RE = re.compile(r"^V[A-Z0-9]{8}$")


# --- create_vk_verification_code / get_verification_by_code -----------------

async def test_create_verification_code_format(temp_db):
    code = await db.create_vk_verification_code(1)
    assert CODE_RE.match(code)


async def test_get_verification_by_code_returns_pending_record(temp_db):
    code = await db.create_vk_verification_code(42)
    record = await db.get_verification_by_code(code)
    assert record is not None
    assert record["tg_user_id"] == 42
    assert record["verified_at"] is None


async def test_get_verification_by_code_unknown_returns_none(temp_db):
    assert await db.get_verification_by_code("VNOPE0000") is None


async def test_get_verification_by_code_expired_returns_none(temp_db):
    code = await db.create_vk_verification_code(1, ttl_minutes=-1)  # уже просрочен
    assert await db.get_verification_by_code(code) is None


async def test_create_verification_code_regenerates_and_overwrites_pending(temp_db):
    code1 = await db.create_vk_verification_code(1)
    code2 = await db.create_vk_verification_code(1)
    assert code1 != code2
    assert await db.get_verification_by_code(code1) is None  # старый код больше не найдётся
    record = await db.get_verification_by_code(code2)
    assert record["tg_user_id"] == 1


async def test_create_verification_code_cleans_up_expired_unverified(temp_db):
    await db.create_vk_verification_code(1, ttl_minutes=-1)
    await db.create_vk_verification_code(2)  # генерация нового кода чистит просроченные

    async with aiosqlite.connect(temp_db) as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM vk_verifications WHERE tg_user_id = 1")
        count = (await cursor.fetchone())[0]
    assert count == 0


async def test_create_verification_code_retries_on_collision(temp_db):
    """Регрессия: если сгенерированный код уже занят (UNIQUE-индекс), функция
    должна тихо перегенерировать, а не упасть непойманным IntegrityError."""
    await db.create_vk_verification_code(1)
    async with aiosqlite.connect(temp_db) as conn:
        await conn.execute(
            "UPDATE vk_verifications SET verification_code = 'VAAAAAAAA' WHERE tg_user_id = 1",
        )
        await conn.commit()

    calls = iter(["A"] * 8 + ["B"] * 8)
    with patch("database.secrets.choice", side_effect=lambda alphabet: next(calls)):
        code = await db.create_vk_verification_code(2)

    assert code == "VBBBBBBBB"


# --- confirm_vk_verification / get_vk_verification --------------------------

async def test_get_vk_verification_none_before_confirm(temp_db):
    await db.create_vk_verification_code(1)
    assert await db.get_vk_verification(1) is None


async def test_confirm_vk_verification_success(temp_db):
    code = await db.create_vk_verification_code(1)
    verification = await db.get_verification_by_code(code)

    await db.confirm_vk_verification(
        tg_user_id=verification["tg_user_id"], vk_user_id=555,
        vk_profile_url="vk.com/id555", vk_full_name="Иван Иванов",
    )

    result = await db.get_vk_verification(1)
    assert result["vk_user_id"] == 555
    assert result["vk_profile_url"] == "vk.com/id555"
    assert result["vk_full_name"] == "Иван Иванов"
    assert result["verified_at"] is not None


async def test_confirm_vk_verification_revokes_previous_owner(temp_db):
    """Если этот же VK-аккаунт верифицируют за другого tg_user_id (сменился
    TG-аккаунт) - старая привязка должна быть снята, а не упасть на UNIQUE."""
    await db.confirm_vk_verification(
        tg_user_id=1, vk_user_id=555, vk_profile_url="vk.com/id555", vk_full_name="Первый",
    )
    await db.confirm_vk_verification(
        tg_user_id=2, vk_user_id=555, vk_profile_url="vk.com/id555", vk_full_name="Второй",
    )

    assert await db.get_vk_verification(1) is None
    result2 = await db.get_vk_verification(2)
    assert result2["vk_user_id"] == 555
    assert result2["vk_full_name"] == "Второй"


# --- sanitize_display_name ---------------------------------------------------

def test_sanitize_display_name_strips_markup_characters():
    assert db.sanitize_display_name("Имя [id1|test]") == "Имя id1test"


def test_sanitize_display_name_empty_falls_back():
    assert db.sanitize_display_name("   ") == "VK User"
    assert db.sanitize_display_name("[]|") == "VK User"


def test_sanitize_display_name_truncates():
    long_name = "И" * 100
    assert len(db.sanitize_display_name(long_name)) == 64
