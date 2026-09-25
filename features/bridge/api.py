"""API фичи «Мост» — REST-стык между расширением браузера и сервером
(включая приём кук веб-чатов, Task 24-a).

Контракт: create_router() -> fastapi.APIRouter; FeatureLoader
монтирует его сам, src/main.py не правится.

Кто ходит сюда:
  * РАСШИРЕНИЕ (service worker / event page):
      POST /api/bridge/tabs            — снапшот открытых вкладок;
      GET  /api/bridge/pull?wait=15    — long-poll задач сервер→расширение
                                         (MV3-воркер спит, WS держать нечем);
      POST /api/bridge/jobs/{id}/ack   — «задачу взял»;
      POST /api/bridge/jobs/{id}/done  — результат (текст страницы/ошибка);
      POST /api/bridge/capture         — положить захват страницы/выделения;
      POST /api/bridge/cookies         — куки веб-чатов (Task 24-a):
                                         расширение открыло страницу входа,
                                         пользователь залогинился, куки
                                         chat.deepseek.com / chat.qwen.ai
                                         уходят сюда → .env (DEEPSEEK_*,
                                         QWEN_WEB_*).
  * ВЕБ-UI (вкладка «Мост» в настройках):
      GET  /api/bridge/status | /tabs | /captures | /jobs;
      POST /api/bridge/read            — «прочитай вкладку сейчас»;
      DELETE /api/bridge/captures/{id}.
  * MCP «bridge» ходит в BridgeStore напрямую (отдельный процесс).

Захваты и задачи публикуются в шину событий (src.events) — web-UI
получает их в чате как {"type":"event","kind":"bridge.capture"}.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.bridge_store import (
    JOB_WAIT_DEFAULT_S,
    BridgeStore,
    get_store,
)
from src.web_cookies import (
    cookies_status,
    save_provider_cookies,
    wipe_provider,
)

logger = logging.getLogger(__name__)

PULL_POLL_S = 0.5        # шаг опроса очереди в long-poll
PULL_WAIT_MAX_S = 45     # потолок wait (браузер сам рвёт на таймауте)


# ВАЖНО: модели — только на уровне модуля. from __future__ import
# annotations делает аннотации строками; FastAPI резолвит их через
# globals модуля, класс внутри create_router() не виден и тело запроса
# молча превращается в query-параметр (422). Как в features/ops/api.py.
class TabsIn(BaseModel):
    tabs: list[dict] = Field(default_factory=list)
    meta: dict = Field(default_factory=dict)


class JobDoneIn(BaseModel):
    ok: bool = True
    result: object = None
    error: str | None = None


class CaptureIn(BaseModel):
    title: str = ""
    url: str = ""
    text: str = ""
    selection: str = ""
    tab_id: int | str | None = None
    source: str = "extension"


class ReadIn(BaseModel):
    tab_id: int | str | None = None
    url_contains: str = ""
    wait_s: float = JOB_WAIT_DEFAULT_S
    max_chars: int = 40000


# Task 24-a: куки веб-чатов из расширения → .env
class CookieItem(BaseModel):
    name: str = ""
    value: str = ""
    domain: str = ""
    path: str = ""
    expirationDate: float | None = None


class CookiesIn(BaseModel):
    provider: str = ""                # deepseek | qwen
    url: str = ""
    cookies: list[CookieItem] = Field(default_factory=list)
    storage: dict[str, str] = Field(default_factory=dict)
    source: str = "extension"


def create_router() -> APIRouter:
    router = APIRouter(prefix="/api/bridge", tags=["bridge"])
    store: BridgeStore = get_store()


    # ── Вкладки ─────────────────────────────────────────────
    @router.post("/tabs")
    async def tabs_register(body: TabsIn):
        """Расширение сообщает снапшот открытых вкладок."""
        res = store.register_tabs(body.tabs, body.meta)
        return res

    @router.get("/tabs")
    async def tabs_list():
        return store.list_tabs()

    # ── Long-poll очередь задач ─────────────────────────────
    @router.get("/pull")
    async def pull(wait: float = 0.0, limit: int = 10):
        """Расширение забирает задачи.

        wait=0 — мгновенный ответ (можно дергать чаще), wait>0 —
        ждём появления pending-задач до wait секунд (long-poll).
        Вместе с задачами отдаём pull_interval — через сколько секунд
        воркеру стоит прийти снова.
        """
        wait = max(0.0, min(wait, PULL_WAIT_MAX_S))
        limit = max(1, min(limit, 20))
        deadline = asyncio.get_event_loop().time() + wait
        while True:
            jobs = store.pull_jobs(limit)
            if jobs or asyncio.get_event_loop().time() >= deadline:
                return {"jobs": jobs, "count": len(jobs),
                        "pull_interval": 30 if not jobs else 1}
            await asyncio.sleep(PULL_POLL_S)

    @router.post("/jobs/{job_id}/ack")
    async def job_ack(job_id: str):
        if not store.ack_job(job_id):
            raise HTTPException(404, "задача не найдена или уже закрыта")
        return {"ok": True}

    @router.post("/jobs/{job_id}/done")
    async def job_done(job_id: str, body: JobDoneIn):
        res = store.complete_job(
            job_id, body.result,
            None if body.ok else (body.error or "ошибка без описания"))
        if not res:
            raise HTTPException(404, "задача не найдена или уже закрыта")
        return {"ok": True}

    @router.get("/jobs")
    async def jobs_list(limit: int = 30):
        return {"jobs": store.list_jobs(limit)}

    # ── «Прочитать вкладку сейчас» (UI / агенты через API) ──
    @router.post("/read")
    async def read_tab(body: ReadIn):
        """Создаёт задачу read_tab и ждёт результат от расширения."""
        job = store.put_job(
            "read_tab",
            payload={"tab_id": body.tab_id,
                     "url_contains": body.url_contains,
                     "max_chars": body.max_chars},
            source="api")
        await _publish("bridge.job", {"id": job["id"], "kind": "read_tab"})
        res = await store.wait_job(job["id"], timeout_s=min(
            max(body.wait_s, 2.0), 120.0))
        return {"job_id": job["id"], **res}

    # ── Захваты ─────────────────────────────────────────────
    @router.post("/capture")
    async def capture(body: CaptureIn):
        rec = store.add_capture(body.model_dump())
        await _publish("bridge.capture", {
            "id": rec["id"], "title": rec["title"], "url": rec["url"],
            "chars": len(rec["text"]), "selection": bool(rec["selection"]),
        })
        return {"ok": True, "id": rec["id"]}

    @router.get("/captures")
    async def captures(q: str = "", limit: int = 50, offset: int = 0):
        return store.list_captures(q=q, limit=min(limit, 200),
                                   offset=max(offset, 0))

    @router.get("/captures/{cap_id}")
    async def capture_get(cap_id: str):
        rec = store.get_capture(cap_id)
        if rec is None:
            raise HTTPException(404, "захват не найден")
        return rec

    @router.delete("/captures/{cap_id}")
    async def capture_delete(cap_id: str):
        if not store.delete_capture(cap_id):
            raise HTTPException(404, "захват не найден")
        return {"ok": True}

    # ── Куки веб-чатов (Task 24-a) ──────────────────────────
    @router.post("/cookies")
    async def cookies_save(body: CookiesIn):
        """Расширение принесло куки чата после логина → в .env + os.environ."""
        try:
            res = save_provider_cookies(
                body.provider, body.url,
                [c.model_dump() for c in body.cookies],
                storage=body.storage, source=body.source)
        except ValueError as e:
            raise HTTPException(422, str(e))
        await _publish("bridge.cookies", {
            "provider": body.provider,
            "saved_env": res["saved_env"],
        })
        return res

    @router.get("/cookies/status")
    async def cookies_status_ep():
        return cookies_status()

    @router.delete("/cookies/{provider}")
    async def cookies_wipe(provider: str):
        try:
            res = wipe_provider(provider)
        except ValueError as e:
            raise HTTPException(404, str(e))
        await _publish("bridge.cookies", {
            "provider": provider, "saved_env": [], "wiped": True})
        return res

    # ── Диагностика ─────────────────────────────────────────
    @router.get("/status")
    async def status():
        s = store.status()
        s["extension"] = "ok" if s["tabs"]["age_s"] < 120 else \
            "не на связи" if s["tabs"]["count"] else "не подключалось"
        return s

    return router


async def _publish(kind: str, payload: dict) -> None:
    """Событие в шину (ws_clients разнесёт его в чат); никогда не бросает."""
    try:
        from src import events
        await events.publish(kind, payload)
    except Exception:
        pass
