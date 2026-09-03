import os
import sys
from pathlib import Path

# Обязательные переменные окружения нужны ДО импорта config.py любым тестовым
# модулем (config.py валидирует их на уровне модуля при импорте).
os.environ.setdefault("TG_BOT_TOKEN", "123456:test_token")
os.environ.setdefault("VK_GROUP_TOKEN", "test_vk_token")
os.environ.setdefault("VK_GROUP_ID", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import database as db


@pytest.fixture
async def temp_db(tmp_path):
    """Свежая изолированная aiosqlite-БД на каждый тест."""
    original_path = db.DB_PATH
    db.DB_PATH = str(tmp_path / "test.db")
    await db.init_db()
    try:
        yield db.DB_PATH
    finally:
        db.DB_PATH = original_path


def make_message(chat_id=100, user_id=999, chat_type="private"):
    msg = MagicMock()
    msg.answer = AsyncMock()
    msg.chat.id = chat_id
    msg.chat.type = chat_type
    msg.from_user.id = user_id
    msg.from_user.username = None
    msg.bot.id = 123456
    msg.bot.get_chat_member = AsyncMock()
    # По умолчанию - обычное текстовое сообщение без медиа/подписи, как в
    # реальном Telegram (иначе MagicMock() делает voice/photo/video truthy
    # сами по себе, что ломает проверки "есть ли медиа" в коде).
    msg.voice = None
    msg.photo = None
    msg.video = None
    msg.caption = None
    return msg


def make_state(chat_id=100, user_id=999):
    """Реальный FSMContext на изолированном MemoryStorage для теста."""
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=0, chat_id=chat_id, user_id=user_id))


async def verify_user(tg_user_id=999, vk_user_id=200, vk_full_name="Тест VK"):
    """Напрямую (в обход кода из /link_vk) отмечает tg_user_id как подтвердившего
    VK-профиль vk_user_id - для тестов, которым верификация нужна только как
    предусловие, а не как то, что проверяется в этом конкретном тесте."""
    await db.confirm_vk_verification(
        tg_user_id=tg_user_id, vk_user_id=vk_user_id,
        vk_profile_url=f"vk.com/id{vk_user_id}", vk_full_name=vk_full_name,
    )
