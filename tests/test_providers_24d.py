"""Task 24-d: расширение списка поддерживаемых LLM.

Проверки:
  1.  config/models.yaml: новые провайдеры (OpenRouter, SiliconFlow,
      VseGPT/ProxyAPI, LM Studio, Anthropic, Gemini, Groq, Mistral),
      selection.fallbacks, docs_url
  2.  resolve_endpoint: квалифицированные ID «provider/model»,
      коллизии имён, умные дефолты Ollama по размеру
  3.  fallback_chain: yaml-цепочка, env-переопределение LLM_FALLBACKS,
      исключение исходной модели, лимит 3
  4.  LLMClient: авто-фолбэк (связь/модель), подсказка в UI,
      отказ от фолбэка при переполнении контекста и начатом стриме
  5.  reasoning_content: сбор в sync и stream путях, on_reasoning
  6.  aggregate(): models_total, docs_url, квалификация имён, cap 200

Запуск: /home/z/.venv/bin/python tests/test_providers_24d.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace as NS

import yaml

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

_ok = 0
_fail = 0
_failures: list[str] = []


def check(cond, name: str) -> None:
    global _ok, _fail
    if cond:
        _ok += 1
    else:
        _fail += 1
        _failures.append(name)
        print(f"  FAIL: {name}")


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 52 - len(title)))


def _async_collect(buf: list[str]):
    async def _cb(t: str) -> None:
        buf.append(t)
    return _cb


# ═══════════════════════════════════════════════════════════════════
from src.llm_providers import (  # noqa: E402
    PROBE_MODEL_CAP, aggregate, endpoint_supports_tools, fallback_chain,
    is_local_model, model_provider, qualify, resolve_endpoint,
    yaml_models_force_reload,
)

yaml_models_force_reload()

section("1. config/models.yaml: новые провайдеры")
cfg = yaml.safe_load((BASE / "config" / "models.yaml").read_text("utf-8"))
prov = cfg.get("providers") or {}
NEW = ["openrouter", "siliconflow", "vsegpt", "proxyapi", "lmstudio",
       "anthropic", "gemini", "groq", "mistral"]
check(set(NEW) <= set(prov), "провайдеры 24-d объявлены в yaml")
check(str(prov.get("openrouter", {}).get("api_key_env"))
      == "OPENROUTER_API_KEY", "openrouter: api_key_env")
check(str(prov.get("vsegpt", {}).get("base_url", "")).startswith("https://api.vsegpt.ru"),
      "vsegpt: base_url")
check(bool(prov.get("lmstudio", {}).get("local")),
      "lmstudio: local=true (без интернета)")
check(all(prov[p].get("docs_url") for p in NEW),
      "у всех новых провайдеров есть docs_url")
sel = cfg.get("selection") or {}
fb = [str(m) for m in (sel.get("fallbacks") or [])]
check(len(fb) >= 2 and fb[-1].startswith("ollama_local/"),
      "selection.fallbacks: локальная модель — последний рубеж")
models_cfg = cfg.get("models") or {}
check(models_cfg.get("openrouter/deepseek/deepseek-chat-v3-0324:free", {})
      .get("provider") == "openrouter",
      "квалифицированный ключ openrouter/... в models")
check(models_cfg.get("siliconflow/Qwen/Qwen3-8B", {}).get("cost_hint")
      == "free", "siliconflow: free-модель объявлена")

section("2. resolve_endpoint: квалифицированные ID")
ep = resolve_endpoint("openrouter/deepseek/deepseek-chat-v3-0324:free")
check(ep is not None and ep[0] == "https://openrouter.ai/api/v1"
      and ep[2].get("provider") == "openrouter",
      "точный квалифицированный ключ из yaml → OpenRouter")
ep = resolve_endpoint("deepseek/deepseek-chat")
check(ep is not None and ep[0] == "https://api.deepseek.com/v1",
      "deepseek/deepseek-chat → DeepSeek API")
ep = resolve_endpoint("openrouter/deepseek-chat")
check(ep is not None and ep[0] == "https://openrouter.ai/api/v1",
      "коллизия: openrouter/deepseek-chat → OpenRouter, не DeepSeek")
check(model_provider("openrouter/deepseek-chat") == "openrouter"
      and model_provider("deepseek/deepseek-chat") == "deepseek",
      "model_provider различает провайдеров по квалификатору")
ep = resolve_endpoint("ollama_local/qwen2.5:7b-instruct")
check(ep is not None and ep[2].get("supports_tools") is True
      and ep[2].get("local") is True,
      "ollama 7b: умный дефолт tools=true")
ep = resolve_endpoint("qwen2.5:3b-instruct")   # эвристика тега, ≥3b
check(ep is not None and ep[2].get("supports_tools") is True,
      "ollama-тег 3b: tools=true (умный дефолт)")
ep = resolve_endpoint("llama3.2:1b")
check(ep is not None and ep[2].get("supports_tools") is False,
      "ollama-тег 1b: tools=false")
check(endpoint_supports_tools("lmstudio/whatever-model"),
      "lmstudio без yaml-записи: tools разрешены (модели 7b+ в GUI)")
check(is_local_model("lmstudio/whatever-model"),
      "lmstudio: локальный провайдер")
ep = resolve_endpoint("nosuchprovider/model-x")
check(ep is None, "неизвестный квалификатор → None (дефолтный клиент)")
ep = resolve_endpoint("anthropic/claude-sonnet-4-5")
check(ep is not None and "api.anthropic.com" in ep[0],
      "anthropic → официальный OpenAI-compat endpoint")
ep = resolve_endpoint("gemini/gemini-2.5-flash")
check(ep is not None and "generativelanguage" in ep[0],
      "gemini → OpenAI-compat endpoint Google")
ep = resolve_endpoint("qwen3.8-max")
check(ep is not None and ep[0] == "http://127.0.0.1:7936/v1",
      "обратная совместимость: короткое имя работает")

section("3. fallback_chain")
saved_env = os.environ.pop("LLM_FALLBACKS", None)
try:
    chain = fallback_chain("deepseek/deepseek-chat")
    check("deepseek/deepseek-chat" not in chain and len(chain) >= 1,
          "исходная модель исключена из цепочки")
    check(chain[-1].startswith("ollama_local/"),
          "yaml-цепочка: локальная модель в конце")
    check(len(fallback_chain()) <= 3, "лимит цепочки — 3 попытки")
    os.environ["LLM_FALLBACKS"] = "fb-a, fb-b , fb-c, fb-d"
    check(fallback_chain("fb-a") == ["fb-b", "fb-c", "fb-d"],
          "env LLM_FALLBACKS переопределяет yaml (пробелы, лимит 3)")
finally:
    if saved_env is not None:
        os.environ["LLM_FALLBACKS"] = saved_env
    else:
        os.environ.pop("LLM_FALLBACKS", None)

# ═══════════════════════════════════════════════════════════════════
section("4. LLMClient: авто-фолбэк")
from src.llm_client import LLMClient  # noqa: E402


class _Delta:
    """Дельта стрима в стиле openai (content/reasoning/tool_calls)."""

    def __init__(self, content=None, rc=None):
        self.content = content
        self.reasoning_content = rc
        self.reasoning = None
        self.tool_calls = None


class FakeCompletions:
    """fake chat.completions.create с маршрутом ошибок по модели."""

    def __init__(self, behavior: dict):
        self.behavior = behavior   # model → "conn" | "ctx" | "stream" | ok
        self.calls: list[str] = []

    def _route(self, model):
        self.calls.append(model)
        b = self.behavior.get(model, "ok")
        return b(model) if callable(b) else b

    async def create(self, **kwargs):
        model = kwargs.get("model", "")
        r = self._route(model)
        if r == "conn":
            raise ConnectionError("connection refused")
        if r == "ctx":
            raise RuntimeError(
                "Error code: 400 - this model's maximum context length "
                "is 4096 tokens, however you requested more")
        if r == "stream":
            # стрим: один токен, потом обрыв связи
            async def _drop_gen():
                yield NS(choices=[NS(delta=_Delta(content="прив"))])
                raise ConnectionError("stream dropped")
            return _drop_gen()
        # ok: sync-ответ или стрим — как просил вызов
        if kwargs.get("stream"):
            async def _ok_gen():
                yield NS(choices=[NS(delta=_Delta(
                    content=f"ответ от {model}"))])
            return _ok_gen()
        return NS(choices=[NS(message=NS(
            content=f"ответ от {model}", reasoning_content=None,
            tool_calls=None))])


def make_llm(behavior: dict) -> tuple[LLMClient, FakeCompletions]:
    llm = LLMClient(cache=None)
    fake = FakeCompletions(behavior)
    # клиент в стиле AsyncOpenAI: client.chat.completions.create(...)
    client = NS(chat=NS(completions=fake))
    llm._client_for = lambda model: (client, model, {"provider": "fake"})
    return llm, fake


os.environ["LLM_FALLBACKS"] = "fb-a,fb-b"
try:
    llm, fake = make_llm({"primary-x": "conn", "fb-a": "ok"})
    res = asyncio.run(llm.chat([{"role": "user", "content": "привет"}],
                               model="primary-x"))
    check(res.get("fallback_from") == "primary-x"
          and res.get("model_used") == "fb-a",
          "фолбэк: result помечен fallback_from/model_used")
    check(fake.calls == ["primary-x", "fb-a"],
          "фолбэк: одна попытка на модель, порядок цепочки")
    check(res.get("content") == "ответ от fb-a",
          "фолбэк: ответ от fallback-модели")

    tokens: list[str] = []
    llm2, _ = make_llm({"primary-x": "conn", "fb-a": "ok"})
    res2 = asyncio.run(llm2.chat([{"role": "user", "content": "привет"}],
                                 model="primary-x",
                                 on_token=_async_collect(tokens)))
    check(any("переключаюсь" in t and "fb-a" in t for t in tokens),
          "фолбэк: в UI уходит подсказка «переключаюсь на …»")

    llm3, fake3 = make_llm({"primary-x": "ctx"})
    raised = False
    try:
        asyncio.run(llm3.chat([{"role": "user", "content": "привет"}],
                              model="primary-x"))
    except RuntimeError:
        raised = True
    check(raised and fake3.calls == ["primary-x"],
          "переполнение контекста: фолбэк не запускается (честная ошибка)")

    tokens4: list[str] = []
    llm4, fake4 = make_llm({"primary-x": "stream", "fb-a": "ok"})
    raised4 = False
    try:
        asyncio.run(llm4.chat([{"role": "user", "content": "привет"}],
                              model="primary-x",
                              on_token=_async_collect(tokens4)))
    except ConnectionError:
        raised4 = True
    check(raised4 and fake4.calls == ["primary-x"],
          "стрим уже отдал токен: фолбэк запрещён (дублей текста нет)")

    llm5, fake5 = make_llm({"primary-x": "ok"})
    res5 = asyncio.run(llm5.chat([{"role": "user", "content": "привет"}],
                                 model="primary-x"))
    check("fallback_from" not in res5,
          "без ошибки фолбэк-меток не появляется")
finally:
    os.environ.pop("LLM_FALLBACKS", None)

# ═══════════════════════════════════════════════════════════════════
section("5. reasoning_content (R1/QwQ/reasoner)")


async def _fake_sync_reasoning(**kwargs):
    return NS(choices=[NS(message=NS(
        content="итог", reasoning_content="шаг 1. шаг 2.",
        tool_calls=None))])


llm6, _fake6 = make_llm({})
# sync-путь: message с reasoning_content
llm6._client_for = lambda model: (
    NS(chat=NS(completions=NS(create=_fake_sync_reasoning))), model,
    {"provider": "fake"})

res6 = asyncio.run(llm6.chat([{"role": "user", "content": "q"}],
                             model="any-model"))
check(res6.get("reasoning") == "шаг 1. шаг 2.",
      "sync: reasoning_content собран в result")

# stream-путь: delta.reasoning_content перед content
reasoned: list[str] = []


class _StreamDelta:
    def __init__(self, rc=None, content=None):
        self.reasoning_content = rc
        self.reasoning = None
        self.content = content
        self.tool_calls = None


class _FakeStream:
    def __init__(self):
        self._items = [
            NS(choices=[NS(delta=_StreamDelta(rc="думаю "))]),
            NS(choices=[NS(delta=_StreamDelta(rc="решаю. "))]),
            NS(choices=[NS(delta=_StreamDelta(content="ответ"))]),
        ]

    def __aiter__(self):
        self._it = iter(self._items)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


async def _fake_stream_create(**kwargs):
    return _FakeStream()


llm7 = LLMClient(cache=None)
llm7._client_for = lambda model: (
    NS(chat=NS(completions=NS(create=_fake_stream_create))), model,
    {"provider": "fake"})
toks7: list[str] = []
res7 = asyncio.run(llm7.chat([{"role": "user", "content": "q"}],
                             model="any-model",
                             on_token=_async_collect(toks7),
                             on_reasoning=_async_collect(reasoned)))
check(res7.get("reasoning") == "думаю решаю. ",
      "stream: reasoning собран в result")
check(reasoned == ["думаю ", "решаю. "],
      "stream: on_reasoning получил дельты по порядку")
check(res7.get("content") == "ответ"
      and toks7 == ["ответ"],
      "stream: reasoning не смешивается с content-токенами")

# ═══════════════════════════════════════════════════════════════════
section("6. aggregate(): новые поля и квалификация")


async def _p_big(base: str) -> list[str]:
    return [f"model-{i}" for i in range(250)]


async def _p_lmstudio(base: str) -> list[str]:
    return ["qwen2.5-7b-instruct", "phi-4"]


async def _p_none(base: str) -> list[str]:
    return []


data = asyncio.run(aggregate(probes={
    "openrouter": _p_big, "lmstudio": _p_lmstudio, "qwen": _p_none,
}))
by_id = {p["id"]: p for p in data["providers"]}
check(len(by_id["openrouter"]["models"]) == PROBE_MODEL_CAP
      and by_id["openrouter"]["models_total"] == 250,
      "cap 200 в ответе + models_total из живого списка")
check(by_id["qwen"]["models_total"] >= 1
      and by_id["qwen"]["models"],
      "probe пуст → фолбэк-список из yaml (вкл. квалифицированные)")
check(by_id["lmstudio"]["local"] is True
      and by_id["lmstudio"]["badge"] == "работает без интернета",
      "lmstudio: живой probe + бейдж локального")
saved_keys: dict[str, str | None] = {}
for k in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
    saved_keys[k] = os.environ.pop(k, None)
try:
    data2 = asyncio.run(aggregate(timeout=0.1))
    by_id2 = {p["id"]: p for p in data2["providers"]}
    check("нет ключа OPENROUTER_API_KEY" in by_id2["openrouter"]["error"],
          "openrouter без ключа: честная причина в UI")
    check(by_id2["anthropic"]["models"]
          and by_id2["anthropic"]["models"][0]["id"] == "claude-sonnet-4-5",
          "anthropic без ключа: yaml-фолбэк с коротким именем модели")
finally:
    for k, v in saved_keys.items():
        if v is not None:
            os.environ[k] = v

check(qualify("deepseek", "deepseek-chat") == "deepseek/deepseek-chat",
      "qualify(): формат provider/model")

# ═══════════════════════════════════════════════════════════════════
print(f"\n{'=' * 60}\nИтог: {_ok} OK, {_fail} FAIL")
if _failures:
    print("Провалены:")
    for f in _failures:
        print(f"  - {f}")
sys.exit(1 if _fail else 0)
