"""Базовый LSP-клиент через subprocess + JSON-RPC."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class LSPClient:
    """Универсальный LSP-клиент для любого language server.

    Использует subprocess + JSON-RPC через stdin/stdout.
    """

    def __init__(
        self,
        command: list[str],
        root_uri: str,
        name: str = "lsp",
    ):
        self.command = command
        self.root_uri = root_uri
        self.name = name
        self.process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._initialized = False
        self._capabilities: dict = {}
        self._opened_docs: set[str] = set()
        self._lock = asyncio.Lock()

    @staticmethod
    def is_available(command: list[str]) -> bool:
        return shutil.which(command[0]) is not None

    async def start(self) -> bool:
        if self.process is not None:
            return True
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        except FileNotFoundError:
            logger.warning("LSP command not found: %s", self.command[0])
            return False
        except Exception as e:
            logger.exception("LSP start failed: %s", e)
            return False

        self._reader_task = asyncio.create_task(self._read_loop())

        # Initialize
        init_result = await self._request("initialize", {
            "processId": os.getpid(),
            "rootUri": self.root_uri,
            "capabilities": {
                "textDocument": {
                    "hover": {"contentFormat": ["markdown", "plaintext"]},
                    "synchronization": {"didSave": True},
                    "publishDiagnostics": {"relatedInformation": True},
                },
                "workspace": {
                    "symbol": {"symbolKind": {"valueSet": list(range(1, 27))}},
                },
            },
        })
        if init_result is None:
            return False

        self._capabilities = (init_result or {}).get("capabilities", {})
        await self._notify("initialized", {})
        self._initialized = True
        logger.info("LSP %s initialized", self.name)
        return True

    async def stop(self):
        if self.process is None:
            return
        try:
            await self._request("shutdown", None, timeout=3)
            await self._notify("exit", None)
        except Exception:
            pass
        try:
            self.process.terminate()
            await asyncio.wait_for(self.process.wait(), timeout=5)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass
        if self._reader_task:
            self._reader_task.cancel()
        self.process = None

    # ═══════════════════════════════════════════════════════
    # JSON-RPC
    # ═══════════════════════════════════════════════════════
    async def _read_loop(self):
        assert self.process
        stream = self.process.stdout
        if stream is None:
            return
        try:
            while True:
                headers = {}
                while True:
                    line = await stream.readline()
                    if not line:
                        return
                    line = line.decode("utf-8", errors="replace").strip()
                    if not line:
                        break
                    if ":" in line:
                        key, val = line.split(":", 1)
                        headers[key.strip().lower()] = val.strip()

                content_length = int(headers.get("content-length", "0"))
                if content_length <= 0:
                    continue

                body = await stream.readexactly(content_length)
                try:
                    msg = json.loads(body)
                except json.JSONDecodeError:
                    continue

                self._handle_message(msg)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.debug("LSP read loop ended: %s", e)

    def _handle_message(self, msg: dict):
        if "id" in msg and "method" not in msg:
            # Response
            fut = self._pending.pop(msg["id"], None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(str(msg["error"])))
                else:
                    fut.set_result(msg.get("result"))
        elif "method" in msg and "id" in msg:
            # Request from server — отвечаем по минимальному
            pass
        # Notifications игнорируем

    async def _send(self, msg: dict):
        assert self.process and self.process.stdin
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self.process.stdin.write(header + body)
        await self.process.stdin.drain()

    async def _request(
        self, method: str, params: Any, timeout: float = 30.0,
    ) -> Any:
        async with self._lock:
            rid = self._next_id
            self._next_id += 1

        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut

        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params

        try:
            await self._send(msg)
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            logger.warning("LSP request timeout: %s", method)
            return None
        except Exception as e:
            self._pending.pop(rid, None)
            logger.debug("LSP request error: %s", e)
            return None

    async def _notify(self, method: str, params: Any):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        try:
            await self._send(msg)
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════
    # High-level API
    # ═══════════════════════════════════════════════════════
    async def open_document(self, path: Path, language_id: str):
        uri = path.as_uri()
        if uri in self._opened_docs:
            return uri
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            return uri
        await self._notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": language_id,
                "version": 1,
                "text": text,
            },
        })
        self._opened_docs.add(uri)
        return uri

    async def did_change(self, path: Path, new_text: str):
        uri = path.as_uri()
        await self._notify("textDocument/didChange", {
            "textDocument": {"uri": uri, "version": 2},
            "contentChanges": [{"text": new_text}],
        })

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def capabilities(self) -> dict:
        return self._capabilities
