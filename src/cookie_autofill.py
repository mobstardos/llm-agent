"""Autofill кук веб-чатов через kimi-webbridge (Фаза 2).

kimi-webbridge (порт 10086) умеет navigate/evaluate/cdp в реальном браузере
Edge с сессиями пользователя — собираем localStorage + куки чата и
сохраняем через src.web_cookies.save_provider_cookies (→ .env + environ).

Вызывается:
  - вручную: POST /api/bridge/cookies/refresh {provider}
  - автоматически: web_chat при «куки протухли» (COOKIE_AUTOFILL=1)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DAEMON = os.getenv("KIMI_WEBBRIDGE_URL", "http://127.0.0.1:10086")
KIMI_BIN = os.path.expanduser(
    r"C:\Users\User\.kimi-webbridge\bin\kimi-webbridge")
_AUTOFILL_LOCK = threading.Lock()
_COOLDOWN: dict[str, float] = {}
COOLDOWN_SEC = 60.0

PROVIDERS: dict[str, dict[str, Any]] = {
    "deepseek": {
        "url": "https://chat.deepseek.com/",
        "domains": ("deepseek.com",),
        "storage_keys": ("userToken",),
    },
    "qwen": {
        "url": "https://chat.qwen.ai/",
        "domains": ("qwen.ai", "aliyun.com", "alibaba.com"),
        "storage_keys": ("token", "userToken"),
    },
}


def autofill_enabled() -> bool:
    return os.getenv("COOKIE_AUTOFILL", "1").strip() not in ("0", "false", "")


def daemon_running() -> bool:
    try:
        r = httpx.get(f"{DAEMON}/status", timeout=3.0)
        return r.status_code == 200 and r.json().get("running") is True
    except Exception:
        return False


def ensure_daemon() -> None:
    """Запустить kimi-webbridge daemon, если ещё не поднят."""
    if daemon_running():
        return
    if not os.path.isfile(KIMI_BIN):
        raise RuntimeError(
            f"kimi-webbridge не найден: {KIMI_BIN} — autofill недоступен")
    subprocess.Popen(
        [KIMI_BIN, "start"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
    )
    for _ in range(20):
        time.sleep(0.5)
        if daemon_running():
            return
    raise RuntimeError("kimi-webbridge daemon не поднялся за 10 секунд")


def kimi_cmd(action: str, args: dict, session: str,
             timeout: float = 60.0) -> dict:
    r = httpx.post(f"{DAEMON}/command",
                   json={"action": action, "args": args, "session": session},
                   timeout=timeout)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"kimi {action}: {json.dumps(body, ensure_ascii=False)[:200]}")
    return body.get("data") or {}


def _jwt_age_sec(tok: str) -> float | None:
    """Возраст JWT по iat (None — не JWT)."""
    if not tok.startswith("eyJ"):
        return None
    try:
        import base64 as _b64
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(_b64.urlsafe_b64decode(payload))
        iat = data.get("iat")
        return float(time.time() - iat) if iat else None
    except Exception:
        return None


def _autofill_sync(provider: str) -> dict:
    """Синхронный autofill: navigate → localStorage → куки → сохранить."""
    cfg = PROVIDERS.get(provider)
    if cfg is None:
        raise ValueError(f"неизвестный провайдер: {provider}")
    ensure_daemon()
    session = f"autofill-{provider}-{int(time.time())}"
    nav = kimi_cmd("navigate", {"url": cfg["url"], "newTab": True,
                                "group_title": "autofill"}, session)
    time.sleep(5)
    try:
        keys = cfg["storage_keys"]
        code = ("JSON.stringify({" + ",".join(
            f"{k}: localStorage.getItem('{k}')" for k in keys
        ) + ", __url: location.href})")
        ls_raw = kimi_cmd("evaluate", {"code": code}, session).get("value") or "{}"
        storage_all = json.loads(ls_raw)
        # Qwen: access token живёт ~15 мин, страница обновляет его при
        # загрузке — если прочитанный старый, ждём обновления SPA.
        for _ in range(3):
            ages = [_jwt_age_sec(str(v)) for v in storage_all.values()
                    if isinstance(v, str)]
            ages = [a for a in ages if a is not None]
            if not ages or max(ages) < 120:
                break
            time.sleep(3)
            ls_raw = kimi_cmd("evaluate", {"code": code}, session).get("value") or "{}"
            storage_all = json.loads(ls_raw)
        page_url = storage_all.pop("__url", cfg["url"])
        storage = {k: v for k, v in storage_all.items() if v}
        logged_in = "/login" not in page_url

        raw = kimi_cmd("cdp", {"method": "Network.getAllCookies", "params": {}},
                       session)
        all_cookies = (raw.get("cookies")
                       or (raw.get("result") or {}).get("cookies") or [])
        mine = [c for c in all_cookies
                if any(str(c.get("domain", "")).lstrip(".").endswith(d)
                       for d in cfg["domains"])]
        if not logged_in and not storage:
            raise RuntimeError(
                f"autofill {provider}: не залогинен на {page_url[:80]} — "
                f"войдите в чат в браузере и повторите")
        from src.web_cookies import save_provider_cookies
        report = save_provider_cookies(
            provider, page_url,
            [{k: c.get(k) for k in
              ("name", "value", "domain", "path", "expirationDate")}
             for c in mine],
            storage, source="autofill",
        )
        return {
            "ok": True, "provider": provider,
            "url": page_url[:120], "logged_in": logged_in,
            "cookies": len(mine),
            "saved_env": report.get("saved_env") or [],
            "accepted": report.get("accepted"),
            "rejected": report.get("rejected"),
        }
    finally:
        try:
            kimi_cmd("close_session", {}, session, timeout=10.0)
        except Exception:
            pass


def autofill(provider: str, force: bool = False) -> dict:
    """Обновить куки провайдера из браузера (с cooldown)."""
    now = time.time()
    if not force:
        last = _COOLDOWN.get(provider, 0.0)
        if now - last < COOLDOWN_SEC:
            raise RuntimeError(
                f"autofill {provider}: недавно уже выполнялся "
                f"(<{int(COOLDOWN_SEC)}с) — повторите позже")
    with _AUTOFILL_LOCK:
        try:
            report = _autofill_sync(provider)
            _COOLDOWN[provider] = time.time()
            logger.info("autofill %s: ok, saved_env=%s", provider,
                        report.get("saved_env"))
            return report
        except Exception as e:
            logger.warning("autofill %s: %s", provider, e)
            raise
