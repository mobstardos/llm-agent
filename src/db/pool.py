"""Connection pool для PostgreSQL через psycopg3.

Windows-совместимость (важно!):
  psycopg в async-режиме не работает на ProactorEventLoop — стандартном
  event loop'е Windows. Менять политику глобально НЕЛЬЗЯ: на selector-лупе
  не работают asyncio-сабпроцессы, а они нужны MCP stdio-серверам.
  Поэтому весь psycopg-трафик выполняется на ВЫДЕЛЕННОМ selector-лупе
  в фоновом потоке-демоне, наружу отдаётся обычный async-API:
  корутины мостятся через run_coroutine_threadsafe, объекты соединений —
  прозрачными прокси (_Bridged). Одинаково работает на Windows/Linux.

Быстрый отказ (fail-fast):
  перед открытием пула делается дешёвый TCP-пробник host:port. Если
  PostgreSQL не слушает порт — psycopg-пул вообще не создаётся: ни
  спама «Psycopg cannot use the 'ProactorEventLoop'», ни вечных
  ретраев, ни утечки пулов-зомби. Неудачный open() закрывает пул.

Все служебные сообщения — без пароля (dsn_view()).
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
import selectors
import socket
import sys
import threading
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

_pool: "DatabasePool | None" = None


class PgUnavailable(RuntimeError):
    """PostgreSQL недоступен (порт закрыт / таймаут / отказ в соединении)."""


def _fmt_exc(e: BaseException) -> str:
    """Понятная причина сбоя вместо пустых скобок '()' у TimeoutError."""
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return "таймаут подключения"
    if isinstance(e, PgUnavailable):
        return str(e)
    if isinstance(e, ConnectionRefusedError):
        return "соединение отклонено (сервер не запущен?)"
    text = str(e).strip()
    return text if text else e.__class__.__name__


def dsn_view(dsn: str) -> str:
    """DSN без учётных данных: 'postgresql://localhost:5432/llmagent'."""
    try:
        from psycopg.conninfo import conninfo_to_dict
        parts = conninfo_to_dict(dsn)
        host = parts.get("host") or "localhost"
        port = parts.get("port") or "5432"
        db = parts.get("dbname") or ""
        return f"{host}:{port}/{db}"
    except Exception:
        # URL-форму чистим вручную, keyword-форму отдаём как есть
        if dsn.startswith(("postgres://", "postgresql://")):
            try:
                from urllib.parse import urlsplit
                u = urlsplit(dsn)
                return f"{u.hostname or 'localhost'}:{u.port or 5432}{u.path or ''}"
            except Exception:
                return "postgresql://<dsn>"
        return "postgresql://<dsn>"


def probe_tcp(dsn: str, timeout: float = 2.0) -> tuple[bool, str]:
    """Дешёвая проверка: слушает ли кто-то host:port из DSN.

    Синхронная (вызывать через asyncio.to_thread). Возвращает
    (True, "") либо (False, "host:port: причина"). Unix-сокеты
    (host начинается с '/') не проверяются — считается доступным.
    """
    try:
        from psycopg.conninfo import conninfo_to_dict
        parts = conninfo_to_dict(dsn)
        hosts = str(parts.get("host") or "localhost").split(",")
        ports = str(parts.get("port") or "5432").split(",")
    except Exception:
        hosts, ports = ["localhost"], ["5432"]

    reasons: list[str] = []
    for i, host in enumerate(hosts):
        port = ports[i] if i < len(ports) else ports[0]
        if host.startswith("/"):                    # unix socket
            return True, ""
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return True, ""
        except socket.timeout:
            reasons.append(f"{host}:{port}: таймаут соединения")
        except ConnectionRefusedError:
            reasons.append(
                f"{host}:{port}: соединение отклонено "
                f"(PostgreSQL не запущен или порт закрыт)"
            )
        except OSError as e:
            reasons.append(f"{host}:{port}: {e.__class__.__name__}: {e}")
    return False, "; ".join(reasons) or "сервер недоступен"


# ═══════════════════════════════════════════════════════════════════════
# Фоновый selector-луп для psycopg (единственный на процесс, refcount)
# ═══════════════════════════════════════════════════════════════════════
class _SelectorLoop:
    """Один selector-луп в потоке-демоне на все пулы (refcount)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._refs = 0

    def acquire(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            self._refs += 1
            if self._loop is None:
                # Windows: selector, а не Proactor (psycopg + без сабпроцессов)
                if sys.platform == "win32":
                    loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
                else:
                    loop = asyncio.new_event_loop()
                self._loop = loop
                threading.Thread(
                    target=self._run, args=(loop,),
                    name="psycopg-selector-loop", daemon=True,
                ).start()
            return self._loop

    def _run(self, loop: asyncio.AbstractEventLoop) -> None:
        try:
            loop.run_forever()
        finally:
            with contextlib.suppress(Exception):
                loop.close()

    def release(self) -> None:
        with self._lock:
            self._refs = max(0, self._refs - 1)
            if self._refs == 0 and self._loop is not None:
                loop, self._loop = self._loop, None
                with contextlib.suppress(Exception):
                    loop.call_soon_threadsafe(self._shutdown, loop)

    @staticmethod
    def _shutdown(loop: asyncio.AbstractEventLoop) -> None:
        """Погасить луп: отменить хвосты (воркеры незакрытых пулов) и stop."""
        with contextlib.suppress(Exception):
            for task in asyncio.all_tasks(loop):
                if not task.done():
                    task.cancel()
        with contextlib.suppress(Exception):
            loop.stop()


_LOOP = _SelectorLoop()


def _call_on(loop: asyncio.AbstractEventLoop, coro):
    """Выполнить корутину на фоновом лупе, дождаться из текущего.

    Отмена ожидающей задачи не отменяет уже запущенную корутину
    на фоновом лупе (она доиграет) — для БД-операций это безопасно.
    """
    return asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, loop))


def _wrap(value: Any, loop: asyncio.AbstractEventLoop) -> Any:
    """Завернуть значение так, чтобы await/async-with работали через мост.

    Обычные значения (dict-строки результатов, числа, bool) проходят
    насквозь без обёрток.
    """
    if inspect.isawaitable(value):
        return _AwaitOnLoop(value, loop)
    if hasattr(value, "__aenter__") and hasattr(value, "__aexit__"):
        return _CMOnLoop(value, loop)
    if callable(value) and not isinstance(value, type):
        return _CallOnLoop(value, loop)
    return value


class _AwaitOnLoop:
    __slots__ = ("_coro", "_loop")

    def __init__(self, coro, loop) -> None:
        self._coro, self._loop = coro, loop

    def __await__(self):
        return _call_on(self._loop, self._coro).__await__()


class _CMOnLoop:
    """async-контекст-менеджер, чьи __aenter__/__aexit__ уходят на мост."""

    __slots__ = ("_cm", "_loop")

    def __init__(self, cm, loop) -> None:
        self._cm, self._loop = cm, loop

    async def __aenter__(self):
        obj = await _call_on(self._loop, self._cm.__aenter__())
        if obj is None or isinstance(
            obj, (bool, int, float, str, bytes, list, dict, tuple),
        ):
            return obj
        # Объекты (курсор, транзакция, соединение) — как _Bridged,
        # чтобы их методы тоже уходили через мост (cur.execute и т.п.)
        return _Bridged(obj, self._loop)

    async def __aexit__(self, et, ev, tb):
        return await _call_on(self._loop, self._cm.__aexit__(et, ev, tb))


class _CallOnLoop:
    """Вызов метода: результат снова заворачивается (_wrap)."""

    __slots__ = ("_fn", "_loop")

    def __init__(self, fn, loop) -> None:
        self._fn, self._loop = fn, loop

    def __call__(self, *args, **kwargs):
        return _wrap(self._fn(*args, **kwargs), self._loop)


class _Bridged:
    """Прокси объекта с другого лупа: каждый доступ уходит через мост.

    Достаточно для паттернов проекта: conn.cursor(), conn.transaction(),
    cur.execute/fetchone/fetchall, cur.description/rowcount.
    """

    __slots__ = ("_obj", "_loop")

    def __init__(self, obj, loop) -> None:
        object.__setattr__(self, "_obj", obj)
        object.__setattr__(self, "_loop", loop)

    def __getattr__(self, name: str):
        obj = object.__getattribute__(self, "_obj")
        loop = object.__getattribute__(self, "_loop")
        return _wrap(getattr(obj, name), loop)


async def _register_vector(conn) -> None:
    """pgvector для соединения (мягко: нет пакета — просто пропускаем)."""
    from pgvector.psycopg import register_vector_async
    await register_vector_async(conn)


# ═══════════════════════════════════════════════════════════════════════
# Пул
# ═══════════════════════════════════════════════════════════════════════
class DatabasePool:
    """Асинхронный пул соединений с pgvector (Windows-safe)."""

    def __init__(
        self, dsn: str, min_size: int = 2, max_size: int = 20,
        autocommit: bool = False, connect_timeout: float = 5.0,
    ):
        self.dsn = dsn
        self.min_size = min_size
        self.max_size = max_size
        self.autocommit = autocommit
        self.connect_timeout = connect_timeout
        self._pool: AsyncConnectionPool | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        if self._pool is not None:
            return

        # 1) Fail-fast: нет слушающего порта — не трогаем psycopg вовсе
        #    (иначе AsyncConnectionPool вечно ретраит в фоне и заваливает
        #    лог предупреждениями psycopg.pool на каждой попытке).
        ok, where = await asyncio.to_thread(probe_tcp, self.dsn)
        if not ok:
            raise PgUnavailable(
                f"нет ответа от {where}. "
                f"Проверьте DSN ({dsn_view(self.dsn)}) или отключите "
                f"PostgreSQL в .env"
            )

        # 2) Selector-луп в фоновом потоке (ProactorEventLoop Windows
        #    несовместим с async-psycopg; глобальную политику менять
        #    нельзя — сломает MCP stdio-сабпроцессы).
        self._loop = _LOOP.acquire()
        pool = AsyncConnectionPool(
            conninfo=self.dsn,
            min_size=self.min_size,
            max_size=self.max_size,
            open=False,
            kwargs={"row_factory": dict_row, "autocommit": self.autocommit},
        )
        try:
            await _call_on(
                self._loop,
                pool.open(wait=True, timeout=self.connect_timeout),
            )
        except BaseException:
            # Убираем пул-зомби: без close() он продолжал бы ретраить
            # в фоне даже после нашего отказа (источник спама pool-N).
            with contextlib.suppress(Exception):
                await _call_on(self._loop, pool.close())
            _LOOP.release()
            self._loop = None
            raise
        self._pool = pool
        logger.info(
            "PostgreSQL pool started: %d-%d (%s, selector-loop)",
            self.min_size, self.max_size, dsn_view(self.dsn),
        )

    async def stop(self) -> None:
        if self._pool is not None and self._loop is not None:
            with contextlib.suppress(Exception):
                await _call_on(self._loop, self._pool.close())
            self._pool = None
            _LOOP.release()
            self._loop = None
            logger.info("PostgreSQL pool stopped")

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Any]:
        if self._pool is None or self._loop is None:
            raise RuntimeError("Pool not started")
        cm = self._pool.connection()
        conn = await _call_on(self._loop, cm.__aenter__())
        try:
            await _call_on(self._loop, _register_vector(conn))
        except Exception as e:                     # нет pgvector/пакета — мягко
            logger.debug("pgvector register: %s", e)
        exc_info: tuple = (None, None, None)
        try:
            yield _Bridged(conn, self._loop)
        except BaseException as e:
            # Исключение тела ДОЛЖНО дойти до CM пула: `async with conn:`
            # внутри него сделает rollback. Иначе получили бы commit-on-error.
            exc_info = (type(e), e, e.__traceback__)
            raise
        finally:
            with contextlib.suppress(Exception):
                await _call_on(self._loop, cm.__aexit__(*exc_info))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Any]:
        async with self.connection() as conn:
            async with conn.transaction():
                yield conn

    async def execute(
        self, query: str, params: tuple | dict | None = None,
    ) -> list[dict]:
        if self._pool is None or self._loop is None:
            raise RuntimeError("Pool not started")

        async def _run() -> list[dict]:
            async with self._pool.connection() as conn:   # type: ignore[union-attr]
                async with conn.cursor() as cur:
                    await cur.execute(query, params)
                    if cur.description:
                        return await cur.fetchall()
                    return []

        # Тело целиком выполняется на фоновом лупе — прокси не нужны
        return await _call_on(self._loop, _run())

    async def execute_one(
        self, query: str, params: tuple | dict | None = None,
    ) -> dict | None:
        rows = await self.execute(query, params)
        return rows[0] if rows else None

    async def health(self) -> bool:
        if self._pool is None or self._loop is None:
            return False

        async def _run() -> None:
            async with self._pool.connection() as conn:   # type: ignore[union-attr]
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1")
                    await cur.fetchone()

        try:
            await _call_on(self._loop, _run())
            return True
        except Exception as e:
            logger.warning("DB health check failed: %s", _fmt_exc(e))
            return False


def get_dsn() -> str:
    direct = os.getenv("DATABASE_URL", "").strip()
    if direct:
        return direct
    host = os.getenv("PG_APP_HOST", "localhost")
    port = os.getenv("PG_APP_PORT", "5432")
    user = os.getenv("PG_APP_USER", "llmagent")
    password = os.getenv("PG_APP_PASSWORD", "secret")
    db = os.getenv("PG_APP_DATABASE", "llmagent")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


async def get_pool() -> DatabasePool:
    global _pool
    if _pool is None:
        _pool = DatabasePool(get_dsn())
        await _pool.start()
    return _pool
