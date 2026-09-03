"""Строгое требование ТЗ: символ длинного тире («—», U+2014) запрещён во всех
пользовательских текстах, сообщениях бота, кнопках и логах. Файлы tests/ сюда
не входят - тестам самим нужно оперировать этим символом, чтобы его проверять.
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EM_DASH = "—"

SOURCE_FILES = [
    "config.py",
    "database.py",
    "main.py",
    "router.py",
    "tg_handlers.py",
    "vk_listener.py",
    "vk_sender.py",
]


def test_no_em_dash_in_source_files():
    offenders = []
    for name in SOURCE_FILES:
        path = PROJECT_ROOT / name
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        if EM_DASH in content:
            lines = [
                i + 1 for i, line in enumerate(content.splitlines()) if EM_DASH in line
            ]
            offenders.append(f"{name}: строки {lines}")

    assert not offenders, "Найден символ '—' (используйте '-' или ':'):\n" + "\n".join(offenders)
