import os
import re
import secrets
import sqlite3
import string
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite

DB_PATH = os.getenv("DB_PATH", "school2.db")

_VERIFICATION_CODE_ALPHABET = string.ascii_uppercase + string.digits
_VERIFICATION_CODE_TTL_MINUTES = 15

# Кэш участников VK-беседы для is_conversation_member_cached: peer_id -> (set
# member_id, время последнего обновления через time.monotonic()). Не персистится
# между рестартами процесса - это ок, просто первый запрос после рестарта
# сходит в VK API заново.
_conversation_member_cache: dict = {}
_CONVERSATION_MEMBER_CACHE_TTL_SECONDS = 300

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bridges (
    vk_peer_id INTEGER PRIMARY KEY,
    tg_chat_id INTEGER,
    link_code TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_bridges_tg_chat_id
    ON bridges (tg_chat_id) WHERE tg_chat_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_bridges_link_code
    ON bridges (link_code) WHERE link_code IS NOT NULL;

CREATE TABLE IF NOT EXISTS messages_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vk_peer_id INTEGER NOT NULL,
    vk_msg_id INTEGER NOT NULL,
    tg_chat_id INTEGER NOT NULL,
    tg_msg_id INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_map_vk
    ON messages_map (vk_peer_id, vk_msg_id);

CREATE INDEX IF NOT EXISTS idx_messages_map_tg
    ON messages_map (tg_chat_id, tg_msg_id);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id TEXT PRIMARY KEY,
    custom_name TEXT
);

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    nickname TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS vk_verifications (
    tg_user_id INTEGER PRIMARY KEY,
    verification_code TEXT,
    code_expires_at TEXT,
    vk_user_id INTEGER,
    vk_profile_url TEXT,
    vk_full_name TEXT,
    verified_at TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_vk_verifications_code
    ON vk_verifications (verification_code) WHERE verification_code IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_vk_verifications_vk_user
    ON vk_verifications (vk_user_id) WHERE vk_user_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS message_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vk_peer_id INTEGER NOT NULL,
    direction TEXT NOT NULL,
    sender_tg_user_id INTEGER,
    sender_vk_user_id INTEGER,
    display_name TEXT NOT NULL,
    text TEXT,
    has_media INTEGER NOT NULL DEFAULT 0,
    tg_msg_id INTEGER,
    vk_msg_id INTEGER,
    created_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_message_history_peer
    ON message_history (vk_peer_id, created_at);

CREATE INDEX IF NOT EXISTS idx_message_history_lookup
    ON message_history (vk_peer_id, tg_msg_id);

CREATE TABLE IF NOT EXISTS user_activity (
    tg_user_id INTEGER NOT NULL,
    vk_peer_id INTEGER NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    last_message_at TIMESTAMP,
    PRIMARY KEY (tg_user_id, vk_peer_id)
);

CREATE INDEX IF NOT EXISTS idx_user_activity_peer ON user_activity (vk_peer_id);
"""


async def init_db() -> None:
    """Создаёт файл БД, схему таблиц и включает WAL для конкурентного доступа."""
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.executescript(_SCHEMA)
        await db.commit()


# --- bridges -----------------------------------------------------------

async def create_bridge(
    vk_peer_id: int,
    tg_chat_id: Optional[int] = None,
    link_code: Optional[str] = None,
) -> None:
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        await db.execute(
            """
            INSERT INTO bridges (vk_peer_id, tg_chat_id, link_code)
            VALUES (?, ?, ?)
            ON CONFLICT(vk_peer_id) DO UPDATE SET
                tg_chat_id = excluded.tg_chat_id,
                link_code = excluded.link_code
            """,
            (vk_peer_id, tg_chat_id, link_code),
        )
        await db.commit()


async def get_bridge_by_vk_peer(vk_peer_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT vk_peer_id, tg_chat_id, link_code FROM bridges WHERE vk_peer_id = ?",
            (vk_peer_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_bridge_by_tg_chat(tg_chat_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT vk_peer_id, tg_chat_id, link_code FROM bridges WHERE tg_chat_id = ?",
            (tg_chat_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_bridge_by_link_code(link_code: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT vk_peer_id, tg_chat_id, link_code FROM bridges WHERE link_code = ?",
            (link_code,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def set_bridge_tg_chat(vk_peer_id: int, tg_chat_id: Optional[int]) -> None:
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        await db.execute(
            "UPDATE bridges SET tg_chat_id = ? WHERE vk_peer_id = ?",
            (tg_chat_id, vk_peer_id),
        )
        await db.commit()


async def complete_bridge_link(vk_peer_id: int, tg_chat_id: int) -> bool:
    """Привязывает TG-чат к мосту и одним запросом гасит одноразовый link_code.

    Возвращает False (без изменений в БД), если tg_chat_id уже занят другим
    активным мостом - на этом стоит частичный UNIQUE-индекс bridges.tg_chat_id,
    без проверки это падало непойманным sqlite3.IntegrityError прямо в хэндлере.
    """
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        try:
            await db.execute(
                "UPDATE bridges SET tg_chat_id = ?, link_code = NULL WHERE vk_peer_id = ?",
                (tg_chat_id, vk_peer_id),
            )
            await db.commit()
            return True
        except sqlite3.IntegrityError:
            return False


async def delete_bridge(vk_peer_id: int) -> None:
    """Удаляет мост и все накопленные для него сопоставления сообщений.

    Без очистки messages_map старые пары (vk_msg_id, tg_chat_id) переживают
    /unlink: если этот же vk_peer_id позже привяжут к ДРУГОМУ tg_chat_id,
    get_tg_message()/get_vk_message() могут вернуть строку, указывающую на
    уже неактуальный чат, и reply/edit на старое сообщение уйдёт не туда.
    """
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        await db.execute("DELETE FROM bridges WHERE vk_peer_id = ?", (vk_peer_id,))
        await db.execute("DELETE FROM messages_map WHERE vk_peer_id = ?", (vk_peer_id,))
        await db.commit()


# --- messages_map --------------------------------------------------------

async def add_message_map(
    vk_peer_id: int,
    vk_msg_id: int,
    tg_chat_id: int,
    tg_msg_id: int,
) -> None:
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        await db.execute(
            """
            INSERT INTO messages_map
                (vk_peer_id, vk_msg_id, tg_chat_id, tg_msg_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (vk_peer_id, vk_msg_id, tg_chat_id, tg_msg_id, datetime.now(timezone.utc)),
        )
        await db.commit()


async def get_tg_message(vk_peer_id: int, vk_msg_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT tg_chat_id, tg_msg_id FROM messages_map
            WHERE vk_peer_id = ? AND vk_msg_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (vk_peer_id, vk_msg_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_vk_message(tg_chat_id: int, tg_msg_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT vk_peer_id, vk_msg_id FROM messages_map
            WHERE tg_chat_id = ? AND tg_msg_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (tg_chat_id, tg_msg_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


# --- user_settings ---------------------------------------------------------

async def get_user_settings(user_id: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id, custom_name FROM user_settings WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def set_user_custom_name(user_id: str, custom_name: Optional[str]) -> None:
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        await db.execute(
            """
            INSERT INTO user_settings (user_id, custom_name)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET custom_name = excluded.custom_name
            """,
            (user_id, custom_name),
        )
        await db.commit()


# --- users (регистрация никнейма при /start, Этап 6) -----------------------

async def get_user_nickname(tg_user_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT nickname FROM users WHERE user_id = ?", (tg_user_id,),
        )
        row = await cursor.fetchone()
        return row["nickname"] if row else None


async def save_user_nickname(tg_user_id: int, nickname: str) -> None:
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        await db.execute(
            """
            INSERT INTO users (user_id, nickname)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET nickname = excluded.nickname
            """,
            (tg_user_id, nickname),
        )
        await db.commit()


# --- вспомогательное (без побочных эффектов) --------------------------------

def sanitize_display_name(name: str, max_len: int = 64) -> str:
    """Убирает символы, ломающие VK-упоминание [id123|Имя] или логи (control-characters),
    обрезает длину. Пустой результат после очистки - "VK User", а не пустая строка."""
    cleaned = re.sub(r"[\[\]|\x00-\x1f\x7f]", "", name or "").strip()
    cleaned = cleaned[:max_len]
    return cleaned or "VK User"


# --- vk_verifications (обязательная верификация владения VK-профилем) -------

async def create_vk_verification_code(tg_user_id: int, ttl_minutes: int = _VERIFICATION_CODE_TTL_MINUTES) -> str:
    """Генерирует одноразовый код "V" + 8 alnum-символов через secrets.choice()
    (это identity-токен, а не просто dedup-код - random тут не годится).

    Перед генерацией лениво чистит просроченные неподтверждённые коды, чтобы
    таблица не росла вечно. UPSERT по tg_user_id: повторный вызов затирает
    предыдущий незавершённый код, но не трогает уже подтверждённую верификацию
    этого же пользователя (её можно перегенерировать заново - это ок).
    """
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        await db.execute(
            "DELETE FROM vk_verifications WHERE code_expires_at < ? AND verified_at IS NULL",
            (now.isoformat(),),
        )
        await db.commit()

        expires_at = (now + timedelta(minutes=ttl_minutes)).isoformat()
        last_error: Optional[Exception] = None
        for _ in range(5):
            code = "V" + "".join(secrets.choice(_VERIFICATION_CODE_ALPHABET) for _ in range(8))
            try:
                await db.execute(
                    """
                    INSERT INTO vk_verifications (tg_user_id, verification_code, code_expires_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(tg_user_id) DO UPDATE SET
                        verification_code = excluded.verification_code,
                        code_expires_at = excluded.code_expires_at
                    """,
                    (tg_user_id, code, expires_at),
                )
                await db.commit()
                return code
            except sqlite3.IntegrityError as e:
                last_error = e
                continue
        raise RuntimeError("Не удалось сгенерировать уникальный код верификации") from last_error


async def get_verification_by_code(code: str) -> Optional[dict]:
    """None, если кода нет ИЛИ он просрочен (code_expires_at в прошлом)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT tg_user_id, verification_code, code_expires_at, vk_user_id,
                   vk_profile_url, vk_full_name, verified_at
            FROM vk_verifications
            WHERE verification_code = ? AND code_expires_at >= ?
            """,
            (code, now_iso),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def confirm_vk_verification(
    tg_user_id: int, vk_user_id: int, vk_profile_url: str, vk_full_name: str,
) -> None:
    """vk_full_name должен приходить уже санитизированным (sanitize_display_name).

    Если этот vk_user_id уже был привязан к ДРУГОМУ tg_user_id - сначала
    обнуляет ту старую привязку, иначе упало бы на UNIQUE-индексе
    idx_vk_verifications_vk_user (человек мог поменять TG-аккаунт).
    """
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        await db.execute(
            """
            UPDATE vk_verifications SET
                vk_user_id = NULL, vk_profile_url = NULL, vk_full_name = NULL, verified_at = NULL
            WHERE vk_user_id = ? AND tg_user_id != ?
            """,
            (vk_user_id, tg_user_id),
        )
        await db.execute(
            """
            INSERT INTO vk_verifications (tg_user_id, vk_user_id, vk_profile_url, vk_full_name, verified_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tg_user_id) DO UPDATE SET
                vk_user_id = excluded.vk_user_id,
                vk_profile_url = excluded.vk_profile_url,
                vk_full_name = excluded.vk_full_name,
                verified_at = excluded.verified_at,
                verification_code = NULL,
                code_expires_at = NULL
            """,
            (tg_user_id, vk_user_id, vk_profile_url, vk_full_name, now),
        )
        await db.commit()


async def get_vk_verification(tg_user_id: int) -> Optional[dict]:
    """None, если строки нет ИЛИ верификация ещё не подтверждена (только запрошен код)."""
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT tg_user_id, vk_user_id, vk_profile_url, vk_full_name, verified_at
            FROM vk_verifications
            WHERE tg_user_id = ? AND verified_at IS NOT NULL
            """,
            (tg_user_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def is_conversation_member_cached(vk_bot, vk_peer_id: int, vk_user_id: int) -> bool:
    """Проверяет, состоит ли vk_user_id в VK-беседе vk_peer_id.

    Личные диалоги (peer_id < 2_000_000_000) не имеют понятия "участники" -
    True сразу, без обращения к API (нужно для совместимости с /link_private).
    Иначе - messages.getConversationMembers с кэшем на 5 минут на peer_id,
    чтобы не долбить VK API на каждое сообщение с "vk ...".
    """
    if vk_peer_id < 2_000_000_000:
        return True

    now = time.monotonic()
    cached = _conversation_member_cache.get(vk_peer_id)
    if cached is not None and now - cached[1] <= _CONVERSATION_MEMBER_CACHE_TTL_SECONDS:
        return vk_user_id in cached[0]

    try:
        result = await vk_bot.api.messages.get_conversation_members(peer_id=vk_peer_id)
        member_ids = {m.member_id for m in result.items}
    except Exception:
        return False  # не смогли проверить - fail closed, это защита пересылки

    _conversation_member_cache[vk_peer_id] = (member_ids, now)
    return vk_user_id in member_ids


# --- message_history / user_activity (полная история + отчёты для админов) --

async def add_message_history(
    vk_peer_id: int,
    direction: str,
    sender_tg_user_id: Optional[int],
    sender_vk_user_id: Optional[int],
    display_name: str,
    text: Optional[str],
    has_media: bool,
    tg_msg_id: Optional[int] = None,
    vk_msg_id: Optional[int] = None,
) -> None:
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        await db.execute(
            """
            INSERT INTO message_history
                (vk_peer_id, direction, sender_tg_user_id, sender_vk_user_id,
                 display_name, text, has_media, tg_msg_id, vk_msg_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                vk_peer_id, direction, sender_tg_user_id, sender_vk_user_id,
                display_name, text, int(has_media), tg_msg_id, vk_msg_id,
                datetime.now(timezone.utc),
            ),
        )
        await db.commit()


async def get_message_history_by_tg_msg(vk_peer_id: int, tg_msg_id: int) -> Optional[dict]:
    """Ищет запись истории по (vk_peer_id, tg_msg_id) - используется /whois: и для
    сообщения, изначально пришедшего из TG (tg_msg_id = оригинал), и для
    сообщения, пришедшего из VK (tg_msg_id = ID пересланной в TG копии)."""
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM message_history
            WHERE vk_peer_id = ? AND tg_msg_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (vk_peer_id, tg_msg_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def bump_user_activity(tg_user_id: int, vk_peer_id: int) -> None:
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        await db.execute(
            """
            INSERT INTO user_activity (tg_user_id, vk_peer_id, message_count, last_message_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(tg_user_id, vk_peer_id) DO UPDATE SET
                message_count = message_count + 1,
                last_message_at = excluded.last_message_at
            """,
            (tg_user_id, vk_peer_id, now),
        )
        await db.commit()


async def get_activity_report(vk_peer_id: int) -> list:
    """Список участников моста с их текущим (не историческим) отображаемым
    именем - тем же приоритетом, что использует router._display_name:
    custom_name -> vk_full_name -> nickname -> tg_user_id как строка."""
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT
                ua.tg_user_id AS tg_user_id,
                ua.message_count AS message_count,
                ua.last_message_at AS last_message_at,
                v.vk_profile_url AS vk_profile_url,
                v.verified_at AS verified_at,
                v.vk_full_name AS vk_full_name,
                us.custom_name AS custom_name,
                u.nickname AS nickname
            FROM user_activity ua
            LEFT JOIN vk_verifications v ON v.tg_user_id = ua.tg_user_id
            LEFT JOIN user_settings us ON us.user_id = CAST(ua.tg_user_id AS TEXT)
            LEFT JOIN users u ON u.user_id = ua.tg_user_id
            WHERE ua.vk_peer_id = ?
            ORDER BY ua.message_count DESC
            """,
            (vk_peer_id,),
        )
        rows = [dict(r) for r in await cursor.fetchall()]

    for row in rows:
        row["display_name"] = (
            row["custom_name"] or row["vk_full_name"] or row["nickname"] or str(row["tg_user_id"])
        )
    return rows
