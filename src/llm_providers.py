"""Реестр LLM-провайдеров (Task 24-c/24-d: выбор модели в чате).

Источник — config/models.yaml:
  providers:  id → {name, base_url (${VAR:default}), api_key_env, local,
              docs_url}
  models:     имя модели → {provider, context_window, supports_tools, ...}

Порядок резолвинга модели (resolve_endpoint):
  1. точное имя из models.yaml (в т.ч. квалифицированные ключи вида
     «openrouter/deepseek/deepseek-r1:free»);
  2. квалифицированный ID «provider/rest» — первый сегмент совпадает
     с id провайдера из yaml (коллизии имён между провайдерами решены:
     deepseek/deepseek-chat → DeepSeek API, openrouter/deepseek-chat →
     OpenRouter);
  3. эвристика ollama-тега «name:NN…» — динамически установленные
     модели Ollama без записи в yaml (умные дефолты по размеру).

Функции:
  resolve_endpoint(model) → (base_url, api_key, meta) | None
      куда слать запрос для конкретной модели (LLMClient использует
      это для per-call роутинга).
  model_provider(model) / is_local_model(model) / endpoint_supports_tools
  fallback_chain(model) → [модели] — авто-фолбэк при недоступности.
  aggregate() → данные для GET /api/models (живые probe'ы провайдеров).

Только stdlib + httpx + yaml; любые ошибки — мягкий фолбэк.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import yaml

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
MODELS_YAML = BASE_DIR / "config" / "models.yaml"

#: провайдер по умолчанию, когда модель ни на что не похожа
FALLBACK_PROVIDER = "qwen"

# ollama-тег вида "qwen2.5:1.5b-instruct", "llama3.2:1b", "phi3.5:3.8b"…
_OLLAMA_TAG_RE = re.compile(
    r"^[a-z0-9][a-z0-9._-]*:[0-9]+[a-z0-9._-]*$", re.IGNORECASE,
)

# размер модели в теге: "7b", "1.5b", "235b-a22b", "30b-a3b"…
_OLLAMA_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)b", re.IGNORECASE)

# Лимит моделей провайдера в ответе /api/models (OpenRouter отдаёт 300+;
# UI делает поиск и «показать ещё» по полному списку)
PROBE_MODEL_CAP = 200

_EXP_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):([^}]*)\}")


def _expand_env(value: str) -> str:
    """${VAR:default} → env или дефолт (${VAR} без дефолта — пусто)."""
    if not isinstance(value, str):
        return value
    value = _EXP_RE.sub(
        lambda m: os.environ.get(m.group(1), m.group(2)), value,
    )
    # ${VAR} без дефолта
    return re.sub(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
        lambda m: os.environ.get(m.group(1), ""), value,
    )


# ═══════════════════════════════════════════════════════════════════════
# Загрузка models.yaml (кэш на 5 секунд — файл читается редко, но
# правки yaml подхватываются без рестарта)
# ═══════════════════════════════════════════════════════════════════════

_cache: dict[str, Any] = {}
_cache_at = 0.0
_CACHE_TTL = 5.0


def _load_yaml(force: bool = False) -> dict:
    global _cache, _cache_at
    now = time.time()
    if not force and _cache and now - _cache_at < _CACHE_TTL:
        return _cache
    data: dict = {}
    try:
        if MODELS_YAML.exists():
            with open(MODELS_YAML, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("models.yaml не читается (%s) — провайдеры по умолчанию", e)
        data = {}
    _cache = {
        "providers": data.get("providers") or {},
        "models": data.get("models") or {},
        "default": data.get("default") or "",
        "selection": data.get("selection") or {},
    }
    _cache_at = now
    return _cache


def yaml_models_force_reload() -> None:
    """Сбросить кэш (для тестов)."""
    _load_yaml(force=True)


# ═══════════════════════════════════════════════════════════════════════
# Резолвинг модели → endpoint
# ═══════════════════════════════════════════════════════════════════════

def _provider_cfg(provider_id: str) -> dict:
    return dict(_load_yaml()["providers"].get(provider_id) or {})


def _ollama_native_base() -> str:
    return os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")


def _resolve_from_yaml(model: str) -> tuple[str, str, dict] | None:
    meta = dict(_load_yaml()["models"].get(model) or {})
    pid = str(meta.get("provider") or "")
    if not pid:
        return None
    p = _provider_cfg(pid)
    if not p:
        return None
    base = _expand_env(str(p.get("base_url", "")))
    key_env = str(p.get("api_key_env") or "")
    key = os.getenv(key_env, "") if key_env else ""
    meta.setdefault("provider", pid)
    meta.setdefault("local", bool(p.get("local")))
    return base, key, meta


def qualify(provider_id: str, model: str) -> str:
    """Квалифицированный ID: provider/model (для UI и сохранения выбора)."""
    return f"{provider_id}/{model}"


def _split_qualified(model: str) -> tuple[str, str] | None:
    """«openrouter/deepseek/deepseek-r1» → (openrouter, deepseek/deepseek-r1).

    Возвращает None, если первый сегмент — не известный провайдер.
    """
    if "/" not in model:
        return None
    pid, rest = model.split("/", 1)
    if not rest or pid not in _load_yaml()["providers"]:
        return None
    return pid, rest


def _ollama_size(tag: str) -> float:
    """Размер модели из тега: qwen2.5:7b-instruct → 7.0; нет → 0."""
    m = _OLLAMA_SIZE_RE.search(tag or "")
    return float(m.group(1)) if m else 0.0


def _apply_ollama_defaults(tag: str, meta: dict) -> None:
    """Умные дефолты для ollama-модели без записи в yaml.

    До 24-d любая модель без yaml-записи считалась без tools (дефолт
    под 1.5b). Теперь: ≥3b — умеет tools и тянет 8k контекст,
    меньше — крошечная, tools отсекаем.
    """
    if meta.get("supports_tools") is None:
        meta["supports_tools"] = _ollama_size(tag) >= 3.0
    if not meta.get("context_window"):
        meta["context_window"] = 8192 if _ollama_size(tag) >= 3.0 else 4096


def _resolve_qualified(model: str) -> tuple[str, str, dict] | None:
    """Резолвинг квалифицированного ID «provider/rest».

    meta берётся у модели «rest» из yaml (если есть), но провайдер и
    local принудительно от квалификатора — модель с тем же именем у
    другого провайдера не должна менять маршрут.
    """
    q = _split_qualified(model)
    if q is None:
        return None
    pid, rest = q
    p = _provider_cfg(pid)
    if not p:
        return None
    base = _expand_env(str(p.get("base_url", "")))
    key_env = str(p.get("api_key_env") or "")
    key = os.getenv(key_env, "") if key_env else ""
    rest_meta = dict(_load_yaml()["models"].get(rest) or {})
    rest_meta.pop("provider", None)          # провайдер — от квалификатора
    meta = {"provider": pid, "local": bool(p.get("local")), **rest_meta}
    if pid == "ollama_local":
        _apply_ollama_defaults(rest, meta)
    meta["qualified"] = True
    return base, key, meta


def resolve_endpoint(model: str | None) -> tuple[str, str, dict] | None:
    """Куда слать запрос для модели.

    Возвращает (base_url, api_key, meta) или None — использовать
    дефолтный клиент LLMClient (LLM_BASE_URL из .env).
    Порядок: точное имя из yaml → квалификатор «provider/rest» →
    эвристика ollama-тега (умные дефолты по размеру).
    """
    if not model:
        return None
    model = str(model).strip()
    if not model:
        return None
    hit = _resolve_from_yaml(model)
    if hit is not None:
        return hit
    hit = _resolve_qualified(model)
    if hit is not None:
        return hit
    # Task 27: веб-чаты по кукам — «deepseek_web/web-chat» / «qwen_web/…».
    # base "web://<pid>" — маркер: запрос ведёт src/web_chat (не OpenAI-клиент).
    if "/" in model:
        pid, _rest = model.split("/", 1)
        cfg = WEB_COOKIE_PROVIDERS.get(pid)
        if cfg is not None:
            return (f"web://{cfg['web']}", "", {
                "provider": pid, "web_provider": cfg["web"],
                "local": False, "supports_tools": False,
                "context_window": 0, "qualified": True,
            })
    if _OLLAMA_TAG_RE.match(model):
        meta: dict = {
            "provider": "ollama_local", "local": True,
            "supports_tools": None, "context_window": 0,
        }
        _apply_ollama_defaults(model, meta)
        meta["qualified"] = False
        return f"{_ollama_native_base()}/v1", "", meta
    return None


def fallback_chain(model: str | None = None) -> list[str]:
    """Цепочка авто-фолбэка: env LLM_FALLBACKS → models.yaml.

    Исходная модель исключается; максимум 3 попытки — иначе чат
    зависает на череде нерабочих провайдеров.
    """
    chain: list[str] = []
    env_chain = os.getenv("LLM_FALLBACKS", "").strip()
    if env_chain:
        chain = [m.strip() for m in env_chain.split(",") if m.strip()]
    else:
        cfg = _load_yaml()
        sel = cfg.get("selection") or {}
        chain = [str(m).strip() for m in (sel.get("fallbacks") or []) if m]
        if not chain and cfg.get("default"):
            chain = [str(cfg["default"])]
    if model:
        chain = [m for m in chain if m and m != model]
    return chain[:3]


def model_provider(model: str | None) -> str | None:
    """id провайдера модели (для бейджа «локально» в UI/WS)."""
    if not model:
        return None
    hit = resolve_endpoint(model)
    return hit[2].get("provider") if hit else None


# ═══════════════════════════════════════════════════════════════════
# Веб-чаты по кукам (Task 27): deepseek_web / qwen_web
# ═══════════════════════════════════════════════════════════════════
# Куки собирает расширение Bridge (Task 24-a). Когда они есть в env —
# провайдер появляется в /api/models; запрос ведёт src/web_chat.web_chat.

WEB_COOKIE_PROVIDERS: dict[str, dict] = {
    "deepseek_web": {
        "name": "DeepSeek (веб-чат по кукам)",
        "env_any": ("DEEPSEEK_AUTH_TOKEN", "DEEPSEEK_DS_SESSION_ID",
                    "DEEPSEEK_SMIDV2"),
        "model_id": "web-chat",
        "model_name": "DeepSeek веб-чат (куки)",
        "badge": "через куки расширения",
        "web": "deepseek",
    },
    "qwen_web": {
        "name": "Qwen (веб-чат по кукам)",
        "env_any": ("QWEN_WEB_TOKEN", "QWEN_WEB_COOKIES"),
        "model_id": "web-chat",
        "model_name": "Qwen веб-чат (куки)",
        "badge": "через куки расширения",
        "web": "qwen",
    },
}


def web_cookie_providers() -> list[dict]:
    """Доступные веб-провайдеры в формате aggregate() — только с куками."""
    out: list[dict] = []
    for pid, cfg in WEB_COOKIE_PROVIDERS.items():
        if not any(os.getenv(e, "").strip() for e in cfg["env_any"]):
            continue
        out.append({
            "id": pid,
            "name": cfg["name"],
            "base_url": f"web://{cfg['web']}",
            "local": False,
            "available": True,
            "error": "",
            "badge": cfg["badge"],
            "docs_url": "",
            "models_total": 1,
            "models": [{
                "id": cfg["model_id"],
                "name": cfg["model_name"],
                "supports_tools": False,
            }],
        })
    return out


def is_local_model(model: str | None) -> bool:
    hit = resolve_endpoint(model)
    return bool(hit and hit[2].get("local"))


def endpoint_supports_tools(model: str | None) -> bool:
    """Модели без записи supports_tools считаются умеющими tools."""
    hit = resolve_endpoint(model)
    if hit is None:
        return True
    meta = hit[2]
    v = meta.get("supports_tools")
    return True if v is None else bool(v)


# ═══════════════════════════════════════════════════════════════════════
# Агрегатор для GET /api/models
# ═══════════════════════════════════════════════════════════════════════

Probe = Callable[[str], Awaitable[list[str]]]


async def _probe_openai_models(
    base_url: str, api_key: str, timeout: float = 1.5,
) -> list[str]:
    """GET {base}/models — общий probe для OpenAI-совместимых провайдеров."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key or 'any'}"},
        )
        r.raise_for_status()
        return [
            m.get("id", "") for m in r.json().get("data", []) if m.get("id")
        ]


async def _probe_ollama_tags(timeout: float = 1.5) -> list[str]:
    """Список установленных моделей Ollama через /api/tags."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(f"{_ollama_native_base()}/api/tags")
        r.raise_for_status()
        return [m.get("name", "") for m in r.json().get("models", [])
                if m.get("name")]


async def aggregate(
    *,
    probes: dict[str, Probe] | None = None,
    timeout: float = 1.5,
) -> dict:
    """Живой опрос всех провайдеров параллельно.

    probes — инъекция для тестов: id → async fn(base_url) -> [ids].
    Возвращает {providers: [...]}, остальные ключи (default/saved)
    добавляет эндпоинт.
    """
    cfg = _load_yaml()
    providers_cfg: dict = cfg["providers"]
    models_cfg: dict = cfg["models"]

    async def _safe_probe(
        pid: str, fn: Probe | None, base: str, reason: str,
    ) -> tuple[bool, list[str], str]:
        if fn is None:
            return False, [], reason
        try:
            ids = await asyncio.wait_for(fn(base), timeout)
            return True, [i for i in ids if i], ""
        except asyncio.TimeoutError:
            return False, [], "таймаут"
        except httpx.ConnectError:
            # соединение отклонено/хост недоступен — сервис фактически
            # не запущен (для локальных: qwenproxy/ollama/lmstudio)
            return False, [], "не запущен"
        except httpx.HTTPStatusError as e:
            return False, [], f"HTTP {e.response.status_code}"
        except Exception as e:
            return False, [], str(e)[:120] or type(e).__name__

    # подготавливаем probe-функции по провайдерам
    effective: dict[str, tuple[dict, str, str, Probe]] = {}
    for pid, p in providers_cfg.items():
        p = dict(p or {})
        base = _expand_env(str(p.get("base_url", "")))
        key_env = str(p.get("api_key_env") or "")
        key = os.getenv(key_env, "") if key_env else ""
        local = bool(p.get("local"))

        if pid == "ollama_local":
            if probes and pid in probes:
                fn: Probe = probes[pid]
            else:
                # probe /api/tags игнорирует base_url из yaml (свой OLLAMA_URL)
                async def fn(_base: str, _t: float = timeout) -> list[str]:
                    return await _probe_ollama_tags(_t)
            needs_key = False
        elif probes and pid in probes:
            fn = probes[pid]
            needs_key = False
        else:
            fn = (lambda b, _b=base, _k=key: _probe_openai_models(_b, _k, timeout))
            needs_key = bool(key_env)

        if needs_key and not key:
            effective[pid] = (p, base, f"нет ключа {key_env} в .env", None)
        else:
            effective[pid] = (p, base, "", fn)

    # параллельный опрос
    names = list(effective.keys())
    results = await asyncio.gather(*[
        _safe_probe(pid, effective[pid][3], effective[pid][1], effective[pid][2])
        for pid in names
    ])

    providers_out: list[dict] = []
    for pid, res in zip(names, results):
        p, base, reason, _fn = effective[pid]
        ok, ids, err = res
        local = bool(p.get("local"))
        # модели провайдера: живой список, иначе — записи из yaml
        # (включая квалифицированные ключи «pid/...»)
        yaml_ids = [
            m.split("/", 1)[1] if m.startswith(f"{pid}/") else m
            for m, meta in models_cfg.items()
            if (meta or {}).get("provider") == pid
        ]
        if ok and ids:
            model_ids = ids[:PROBE_MODEL_CAP]
            total = len(ids)
        else:
            model_ids = yaml_ids
            total = len(yaml_ids)

        def _tools_for(mid: str) -> bool:
            meta = (models_cfg.get(mid)
                    or models_cfg.get(qualify(pid, mid)) or {})
            v = meta.get("supports_tools")
            return True if v is None else bool(v)

        entry = {
            "id": pid,
            "name": str(p.get("name") or pid),
            "base_url": base,
            "local": local,
            "available": ok,
            "error": "" if ok else (reason or err or "недоступен"),
            "badge": (
                "работает без интернета" if ok and local
                else "готов" if ok
                else "недоступен"
            ),
            "docs_url": str(p.get("docs_url") or ""),
            "models_total": total,
            "models": [
                {
                    "id": m,
                    "name": m,
                    "supports_tools": _tools_for(m),
                }
                for m in model_ids
            ],
        }
        providers_out.append(entry)

    # Task 27: веб-чаты по кукам — всегда «доступны», когда куки в env
    providers_out.extend(web_cookie_providers())
    return {"providers": providers_out}
