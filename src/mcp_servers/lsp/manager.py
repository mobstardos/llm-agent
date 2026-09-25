"""Менеджер LSP-серверов: по одному на язык, ленивая инициализация."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from src.mcp_servers.lsp.client import LSPClient

logger = logging.getLogger(__name__)


# Команды LSP-серверов по языкам
LSP_COMMANDS = {
    "python": [
        ["pyright-langserver", "--stdio"],
        ["pylsp"],
        ["basedpyright-langserver", "--stdio"],
    ],
    "javascript": [
        ["typescript-language-server", "--stdio"],
    ],
    "typescript": [
        ["typescript-language-server", "--stdio"],
    ],
}


LANG_BY_EXT = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
}


class LSPManager:
    def __init__(self, root: Path):
        self.root = root
        self.root_uri = root.as_uri()
        self._clients: dict[str, LSPClient] = {}

    def language_for(self, path: Path) -> str:
        return LANG_BY_EXT.get(path.suffix.lower(), "")

    def detect_available(self) -> dict[str, list[str]]:
        """Какие LSP-серверы доступны."""
        available: dict[str, list[str]] = {}
        for lang, variants in LSP_COMMANDS.items():
            for cmd in variants:
                if shutil.which(cmd[0]):
                    available.setdefault(lang, []).append(cmd[0])
        return available

    async def get_client(self, language: str) -> LSPClient | None:
        if language in self._clients:
            client = self._clients[language]
            if client.initialized:
                return client

        variants = LSP_COMMANDS.get(language, [])
        for cmd in variants:
            if not shutil.which(cmd[0]):
                continue
            client = LSPClient(cmd, self.root_uri, name=language)
            ok = await client.start()
            if ok:
                self._clients[language] = client
                return client
            await client.stop()

        return None

    async def close_all(self):
        for client in list(self._clients.values()):
            try:
                await client.stop()
            except Exception:
                pass
        self._clients.clear()
