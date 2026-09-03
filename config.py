import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


def _require_str(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Отсутствует обязательная переменная окружения: {name}")
    return value


def _optional_str(name: str) -> Optional[str]:
    value = os.getenv(name, "").strip()
    return value or None


def _require_int(name: str) -> int:
    raw = _require_str(name)
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(
            f"Переменная окружения {name} должна быть целым числом, получено: {raw!r}"
        )


@dataclass(frozen=True)
class Config:
    tg_bot_token: str
    vk_group_token: str
    vk_group_id: int
    tg_api_base_url: Optional[str]


def load_config() -> Config:
    return Config(
        tg_bot_token=_require_str("TG_BOT_TOKEN"),
        vk_group_token=_require_str("VK_GROUP_TOKEN"),
        vk_group_id=_require_int("VK_GROUP_ID"),
        # Опционально: базовый URL прокси перед api.telegram.org (например, свой
        # Cloudflare Worker) - нужен там, где Telegram Bot API заблокирован
        # напрямую (например, на серверах в РФ). Если не задан - идём напрямую.
        tg_api_base_url=_optional_str("TG_API_BASE_URL"),
    )


config = load_config()
