# -*- coding: utf-8 -*-
"""Вложения чата: лимиты, whitelist расширений, безопасные пути, манифест.

Используется дважды:
  - src/routes/files.py — upload/preview (валидация, .inbox);
  - src/ws/chat.py      — build_attachments_block() для промпта.

Безопасность: клиентский payload с путями НЕ доверен — каждый путь
проходит safe_resolve() и обязан лежать внутри <workspace>/.inbox.
"""
from __future__ import annotations

import re
from pathlib import Path

MAX_FILES_PER_REQUEST = 5
MAX_FILE_BYTES = 10 * 1024 * 1024       # 10 МБ на файл
MAX_TOTAL_BYTES = 30 * 1024 * 1024      # 30 МБ суммарно за запрос
INLINE_LIMIT = 32 * 1024                # ≤32 КБ текст встраиваем в промпт

# Whitelist расширений (документы, изображения, текст/данные/код)
ALLOWED_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
    ".pdf", ".docx", ".xlsx", ".pptx",
    ".txt", ".md", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".xml", ".sql",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".scss",
    ".sh", ".bash", ".ps1", ".bat", ".go", ".rs", ".java", ".c", ".cpp",
    ".h", ".rb", ".php", ".r", ".ipynb", ".env", ".rst",
}

# Расширения, содержимое которых безопасно встраивать в промпт как есть
TEXT_SUFFIXES = {
    ".txt", ".md", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf", ".xml", ".sql",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".scss",
    ".sh", ".bash", ".ps1", ".bat", ".go", ".rs", ".java", ".c", ".cpp",
    ".h", ".rb", ".php", ".r", ".env", ".rst",
}

_UNSAFE_RE = re.compile(r"[^\w\.\-\(\)\[\] ]+")


def project_root() -> Path | None:
    """Текущая рабочая папка (PROJECT_ROOT), None если не задана/нет папки."""
    try:
        from src.config import get_settings
        from src.runtime_config import get_project_root
        raw = get_project_root(default=get_settings().project_root)
        if raw:
            p = Path(str(raw)).expanduser()
            if p.is_dir():
                return p.resolve()
    except Exception:
        pass
    return None


def inbox_dir() -> Path | None:
    root = project_root()
    return (root / ".inbox") if root else None


def safe_resolve(path: str) -> Path | None:
    """Абсолютный путь → должен лежать внутри .inbox (анти path-traversal)."""
    inbox = inbox_dir()
    if inbox is None or not path:
        return None
    try:
        p = Path(str(path)).expanduser().resolve()
        inbox_r = inbox.resolve()
        if p == inbox_r or inbox_r in p.parents:
            return p
    except Exception:
        return None
    return None


def sanitize_name(name: str) -> str:
    """Только базовое имя, без директорий и небезопасных символов."""
    base = Path(str(name).replace("\\", "/")).name.strip()
    base = _UNSAFE_RE.sub("_", base).strip("._") or "file"
    return base[:100]


def validate_file(name: str, size: int) -> str:
    """Пустая строка = ок, иначе причина отказа."""
    suffix = Path(str(name)).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        return (f"недопустимое расширение «{suffix or 'нет'}» "
                f"(разрешены: {', '.join(sorted(ALLOWED_SUFFIXES))})")
    if size <= 0:
        return "пустой файл"
    if size > MAX_FILE_BYTES:
        return f"больше {MAX_FILE_BYTES // (1024 * 1024)} МБ"
    return ""


def fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} Б"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} КБ"
    return f"{n / (1024 * 1024):.1f} МБ"


def build_attachments_block(attachments: list) -> str:
    """Текстовый блок для промпта: манифест + инлайн мелкого текста.

    Пути из клиентского payload не доверяем: проходим через
    safe_resolve() (только .inbox). Текст ≤ INLINE_LIMIT встраивается
    целиком, остальное — только путь (агент прочитает через filesystem).
    """
    if not attachments:
        return ""
    lines: list[str] = ["[Вложения пользователя — файлы в .inbox/ рабочей папки]"]
    for a in attachments:
        if not isinstance(a, dict):
            continue
        raw_path = str(a.get("path") or "").strip()
        target = safe_resolve(raw_path)
        if target is None or not target.is_file():
            continue  # вне .inbox / не существует — игнорируем
        name = str(a.get("name") or "").strip() or target.name
        try:
            size = int(a.get("size") or 0)
        except Exception:
            size = target.stat().st_size
        lines.append(f"- {name} ({fmt_size(size)}): {target}")
        if target.suffix.lower() in TEXT_SUFFIXES and 0 < size <= INLINE_LIMIT:
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            lines.append(f"--- начало {name} ---\n{text}\n--- конец {name} ---")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)
