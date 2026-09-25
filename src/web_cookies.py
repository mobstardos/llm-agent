"""Куки веб-чатов DeepSeek/Qwen — приём из расширения, сохранение в .env.

Task 24-a. Расширение «LLM Agent Bridge» (permission «cookies») открывает
страницы чатов, пользователь логинится, расширение собирает куки
(chrome.cookies.getAll) и localStorage (scripting.executeScript) и шлёт их
на POST /api/bridge/cookies. Здесь они:

  1. валидируются (допускаем только куки доменов провайдера);
  2. маппятся на переменные окружения:
       DeepSeek:  ds_session_id  → DEEPSEEK_DS_SESSION_ID
                  smidV2         → DEEPSEEK_SMIDV2
                  .thumbcache_*  → DEEPSEEK_THUMBCACHE
                  localStorage userToken → DEEPSEEK_AUTH_TOKEN
                  (userToken бывает JSON {"token": "..."} — разворачиваем)
       Qwen:      все куки       → QWEN_WEB_COOKIES  (строка Cookie-заголовка)
                  cookie token   → QWEN_WEB_TOKEN
  3. пишутся в .env (src.env_file) и в os.environ текущего процесса —
     deepseek-MCP (подпроцесс) подхватит при следующем старте сервера,
     остальные подсистемы — сразу;
  4. статус (без значений!) — data/bridge/cookies_status.json для
     GET /api/bridge/cookies/status и вкладки «Мост».

Безопасность: значения кук возвращаются только в момент приёма; в статусе
и логах — только имена и маскированные хвосты (…последние 4 символа).
DELETE-эндпоинт затирает переменные из .env и окружения.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from src.env_file import apply_to_environ, project_root, read_env, \
    write_env_updates

logger = logging.getLogger(__name__)

STATUS_FILE = "cookies_status.json"
COOKIE_VALUE_MAX = 4096          # страховка размера значения
COOKIES_MAX = 60                 # максимум кук за раз
STORAGE_VALUE_MAX = 8192

# ── Реестр провайдеров ────────────────────────────────────────────────
# cookie_domains — суффиксы доменов, чужие куки отбрасываем;
# key_cookies — маркеры «пользователь авторизован» (хватает любого);
# storage_keys — что читать из localStorage на странице чата.

THUMBCACHE_RE = re.compile(r"^\.thumbcache_[0-9a-f]{8,}$")


class WebProvider:
    __slots__ = ("pid", "label", "login_url", "cookie_domains",
                 "key_cookies", "storage_keys", "url_contains")

    def __init__(self, pid: str, label: str, login_url: str,
                 cookie_domains: list[str], key_cookies: list[str],
                 storage_keys: list[str], url_contains: str = ""):
        self.pid = pid
        self.label = label
        self.login_url = login_url
        self.cookie_domains = cookie_domains
        self.key_cookies = key_cookies
        self.storage_keys = storage_keys
        self.url_contains = url_contains or login_url.split("/")[2]


PROVIDERS: dict[str, WebProvider] = {
    "deepseek": WebProvider(
        pid="deepseek",
        label="DeepSeek",
        login_url="https://chat.deepseek.com/",
        cookie_domains=["deepseek.com"],
        key_cookies=["ds_session_id", "userToken"],
        storage_keys=["userToken"],
    ),
    "qwen": WebProvider(
        pid="qwen",
        label="Qwen",
        login_url="https://chat.qwen.ai/",
        cookie_domains=["qwen.ai", "aliyun.com", "alibaba.com"],
        key_cookies=["token", "tongyi_sso_ticket", "login_aliyunid_ticket",
                     "smidV2"],
        storage_keys=["token", "userToken"],
    ),
}

# Куки, которые не имеют ценности для API (шумная аналитика) — не пишем.
_NOISE_RE = re.compile(
    r"^(intercom|_ga|_gid|_gat|amp_|Hm_|hm_|ajs_|ajs_|optimizely|"
    r"__stripe|cf_|__cf| Disabilities)", re.IGNORECASE)


def _domain_ok(cookie_domain: str, provider: WebProvider) -> bool:
    d = (cookie_domain or "").lstrip(".").lower()
    if not d:
        return True            # расширение присылает домен не всегда
    return any(d == dom or d.endswith("." + dom) or d.endswith(dom)
               for dom in provider.cookie_domains)


def _clean_user_token(value: str) -> str:
    """userToken из localStorage бывает JSON-объектом {"token": "..."}."""
    v = (value or "").strip()
    if v.startswith("{") and v.endswith("}"):
        try:
            obj = json.loads(v)
            if isinstance(obj, dict) and isinstance(obj.get("token"), str):
                return obj["token"].strip()
        except Exception:
            pass
    return v


def _mask(value: str) -> str:
    v = str(value or "")
    if len(v) <= 8:
        return "…" + ("*" * len(v))
    return f"…{v[-4:]} ({len(v)} симв.)"


def filter_cookies(provider_id: str,
                   cookies: list[dict]) -> tuple[list[dict], list[str]]:
    """Отбирает куки провайдера; возвращает (принятые, отклонённые имена)."""
    prov = PROVIDERS.get(provider_id)
    if prov is None:
        raise ValueError(f"неизвестный провайдер: {provider_id}")
    keep: list[dict] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name", "")).strip()
        value = str(c.get("value", "")).strip()
        if not name or not value:
            continue
        if _NOISE_RE.match(name):
            rejected.append(name)
            continue
        if not _domain_ok(str(c.get("domain", "")), prov):
            rejected.append(name)
            continue
        if len(value) > COOKIE_VALUE_MAX:
            rejected.append(name)
            continue
        if name in seen:               # первое вхождение главнее
            continue
        seen.add(name)
        keep.append({"name": name, "value": value,
                     "domain": str(c.get("domain", "")).lstrip("."),
                     "expires": c.get("expirationDate")
                     or c.get("expires") or None})
        if len(keep) >= COOKIES_MAX:
            break
    return keep, rejected


def env_mapping(provider_id: str, cookies: list[dict],
                storage: dict[str, str]) -> dict[str, str]:
    """Куки + localStorage → переменные окружения (.env)."""
    by_name = {c["name"]: c["value"] for c in cookies}
    updates: dict[str, str] = {}

    if provider_id == "deepseek":
        if by_name.get("ds_session_id"):
            updates["DEEPSEEK_DS_SESSION_ID"] = by_name["ds_session_id"]
        if by_name.get("smidV2"):
            updates["DEEPSEEK_SMIDV2"] = by_name["smidV2"]
        thumb = next((v for k, v in by_name.items()
                      if THUMBCACHE_RE.match(k)), "")
        if thumb:
            updates["DEEPSEEK_THUMBCACHE"] = thumb
        token = _clean_user_token(
            storage.get("userToken") or by_name.get("userToken") or "")
        if token:
            updates["DEEPSEEK_AUTH_TOKEN"] = token

    elif provider_id == "qwen":
        header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        if header:
            updates["QWEN_WEB_COOKIES"] = header
        token = _clean_user_token(
            storage.get("token") or storage.get("userToken")
            or by_name.get("token") or "")
        if token:
            updates["QWEN_WEB_TOKEN"] = token

    return updates


def required_missing(provider_id: str, cookies: list[dict],
                     storage: dict[str, str]) -> list[str]:
    """Есть ли вообще авторизационные маркеры (иначе это просто шум)."""
    prov = PROVIDERS.get(provider_id)
    if prov is None:
        return [provider_id]
    names = {c["name"] for c in cookies}
    names.update(k for k in (storage or {}) if (storage or {}).get(k))
    if provider_id == "deepseek":
        if names & {"ds_session_id", "userToken"}:
            return []
        return ["ds_session_id или userToken"]
    if provider_id == "qwen":
        if names & {"token", "tongyi_sso_ticket", "login_aliyunid_ticket",
                    "smidV2"} or storage.get("token"):
            return []
        return ["token/SSO-кука"]
    return []


def _status_path() -> Path:
    return project_root() / "data" / "bridge" / STATUS_FILE


def _load_status() -> dict:
    try:
        return json.loads(_status_path().read_text(encoding="utf-8"))
    except Exception:
        return {"providers": {}}


def _save_status(status: dict) -> None:
    p = _status_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, p)


_LOCK = threading.Lock()


def save_provider_cookies(provider_id: str, url: str,
                          cookies: list[dict],
                          storage: dict[str, str] | None = None,
                          source: str = "extension") -> dict:
    """Главная точка входа API: принять, проверить, сохранить, отчитаться."""
    prov = PROVIDERS.get(provider_id)
    if prov is None:
        raise ValueError(f"неизвестный провайдер: {provider_id}")

    storage = {str(k)[:64]: str(v)[:STORAGE_VALUE_MAX]
               for k, v in (storage or {}).items()}
    keep, rejected = filter_cookies(provider_id, cookies)

    missing = required_missing(provider_id, keep, storage)
    if missing:
        raise ValueError(
            f"нет авторизационных данных: {', '.join(missing)}. "
            f"Откройте {prov.login_url} через кнопку расширения и войдите.")

    updates = env_mapping(provider_id, keep, storage)
    if not updates:
        raise ValueError("куки приняты, но ни одна переменная не маппится")

    with _LOCK:
        report = write_env_updates(updates)
        apply_to_environ(updates)

        status = _load_status()
        now = time.time()
        status.setdefault("providers", {})[provider_id] = {
            "label": prov.label,
            "ts": now,
            "source": source,
            "url": str(url or "")[:300],
            "env_keys": sorted(updates),
            "cookie_names": sorted(c["name"] for c in keep),
            "rejected": len(rejected),
            "masked": {k: _mask(v)[:48] for k, v in updates.items()},
        }
        _save_status(status)

    logger.info("Куки %s сохранены: %s", provider_id,
                ", ".join(sorted(updates)))
    return {
        "ok": True,
        "provider": provider_id,
        "label": prov.label,
        "saved_env": sorted(updates),
        "accepted": len(keep),
        "rejected": len(rejected),
        "env_report": report,
        "ts": time.time(),
    }


def wipe_provider(provider_id: str) -> dict:
    """Затирает переменные провайдера в .env (пусто) и из os.environ."""
    prov = PROVIDERS.get(provider_id)
    if prov is None:
        raise ValueError(f"неизвестный провайдер: {provider_id}")

    env_keys = ["DEEPSEEK_DS_SESSION_ID", "DEEPSEEK_SMIDV2",
                "DEEPSEEK_THUMBCACHE", "DEEPSEEK_AUTH_TOKEN",
                "QWEN_WEB_COOKIES", "QWEN_WEB_TOKEN"]
    if provider_id == "deepseek":
        env_keys = [k for k in env_keys if k.startswith("DEEPSEEK_")]
    else:
        env_keys = [k for k in env_keys if k.startswith("QWEN_")]

    # write_env_updates не пишет пустые — правим файл вручную под замком.
    from src.env_file import env_path
    p = env_path()
    with _LOCK:
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            lines = []
        out: list[str] = []
        for line in lines:
            stripped = line.strip()
            key = stripped.split("=", 1)[0].strip() if \
                "=" in stripped and not stripped.startswith("#") else ""
            if key in env_keys:
                out.append(f"{key}=")
            else:
                out.append(line)
        tmp = p.with_suffix(".env.tmp")
        tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
        os.replace(tmp, p)
        for k in env_keys:
            os.environ.pop(k, None)

        status = _load_status()
        if provider_id in status.get("providers", {}):
            del status["providers"][provider_id]
            _save_status(status)

    return {"ok": True, "provider": provider_id, "cleared": env_keys}


def cookies_status() -> dict:
    """Статус по провайдерам — БЕЗ значений, только факт и свежесть."""
    status = _load_status()
    env = {**read_env(), **{k: v for k, v in os.environ.items()
                            if k.startswith(("DEEPSEEK_", "QWEN_WEB"))}}
    providers: dict[str, dict] = {}
    now = time.time()
    for pid, prov in PROVIDERS.items():
        rec = status.get("providers", {}).get(pid, {})
        present = [k for k in rec.get("env_keys", []) if env.get(k)]
        env_keys_expected = {
            "deepseek": ["DEEPSEEK_DS_SESSION_ID", "DEEPSEEK_SMIDV2",
                         "DEEPSEEK_THUMBCACHE", "DEEPSEEK_AUTH_TOKEN"],
            "qwen": ["QWEN_WEB_COOKIES", "QWEN_WEB_TOKEN"],
        }[pid]
        in_env = [k for k in env_keys_expected if env.get(k)]
        ts = float(rec.get("ts") or 0)
        providers[pid] = {
            "label": prov.label,
            "configured": bool(in_env) or bool(present),
            "login_url": prov.login_url,
            "updated_at": ts,
            "age_min": round((now - ts) / 60, 1) if ts else None,
            "env_present": in_env,
            "cookie_names": rec.get("cookie_names", []),
            "source": rec.get("source", ""),
        }
    return {"providers": providers, "note":
            "значения кук не возвращаются — только факт настройки"}
