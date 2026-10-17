"""Task 24-c: выбор модели в чате + локальные фоновые микрозадачи.

Проверки:
  1.  config/models.yaml: провайдеры и новые модели
  2.  resolve_endpoint / model_provider / is_local_model / supports_tools
  3.  aggregate(): живые probe'ы, фолбэк-списки, «нет ключа»
  4.  LLMClient: роутинг endpoint'ов + отсечение tools у локальной модели
  5.  ConversationSession: title-записи (авто/ручной) + messages_view
  6.  scan_sessions(): список чатов с заголовками
  7.  MicroTasksWorker: заголовки (модель/фолбэк/без дублей),
      ручной заголовок не перезаписывается, теги с state-смещением,
      дайджест (журнал/шина), персистентность state
  8.  RuntimeConfig: roundtrip model_preferences

Запуск: /home/z/.venv/bin/python tests/test_models_micro.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

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


# ═══════════════════════════════════════════════════════════════════
from src.llm_providers import (
    aggregate, endpoint_supports_tools, is_local_model, model_provider,
    resolve_endpoint, yaml_models_force_reload,
)

yaml_models_force_reload()

section("1. config/models.yaml")
cfg = yaml.safe_load((BASE / "config" / "models.yaml").read_text("utf-8")) \
    if False else None
import yaml as _yaml  # noqa: E402
cfg = _yaml.safe_load((BASE / "config" / "models.yaml").read_text("utf-8"))
prov = cfg.get("providers") or {}
check(set(["qwen", "deepseek", "ollama_local", "openai"]) <= set(prov),
      "провайдеры qwen/deepseek/ollama_local/openai объявлены")
check(bool(prov.get("ollama_local", {}).get("local")),
      "ollama_local помечен local: true")
check(str(prov.get("deepseek", {}).get("api_key_env")) == "DEEPSEEK_API_KEY",
      "deepseek: api_key_env=DEEPSEEK_API_KEY")
models_cfg = cfg.get("models") or {}
check(models_cfg.get("deepseek-chat", {}).get("provider") == "deepseek",
      "модель deepseek-chat → провайдер deepseek")
check(models_cfg.get("qwen2.5:1.5b-instruct", {}).get("provider")
      == "ollama_local",
      "модель qwen2.5:1.5b-instruct → ollama_local")
check(models_cfg.get("qwen2.5:1.5b-instruct", {}).get("supports_tools")
      is False,
      "qwen2.5:1.5b-instruct: supports_tools=false")
check(models_cfg.get("qwen2.5:1.5b-instruct", {}).get("context_window")
      == 4096,
      "qwen2.5:1.5b-instruct: context_window=4096")

section("2. resolve_endpoint / провайдер модели")
ep = resolve_endpoint("qwen3.8-max")
check(ep is not None and ep[0] == "http://127.0.0.1:7936/v1",
      "qwen3.8-max → qwenproxy /v1")
ep = resolve_endpoint("deepseek-chat")
check(ep is not None and ep[0] == "https://api.deepseek.com/v1",
      "deepseek-chat → api.deepseek.com/v1")
ep = resolve_endpoint("qwen2.5:1.5b-instruct")
check(ep is not None and ep[0].endswith("127.0.0.1:11434/v1")
      and ep[2].get("local") is True,
      "qwen2.5:1.5b-instruct → Ollama /v1, local=true")
ep = resolve_endpoint("llama3.2:1b")     # ollama-тег без записи в yaml
check(ep is not None and "11434/v1" in ep[0]
      and ep[2].get("provider") == "ollama_local",
      "эвристика ollama-тега: llama3.2:1b → Ollama")
check(resolve_endpoint("gpt-4o") is None,
      "неизвестная модель → None (дефолтный провайдер)")
check(model_provider("qwen2.5:1.5b-instruct") == "ollama_local",
      "model_provider → ollama_local")
check(is_local_model("qwen2.5:1.5b-instruct")
      and not is_local_model("qwen3.8-max"),
      "is_local_model: ollama — да, qwenproxy — нет")
check(endpoint_supports_tools("qwen3.8-max")
      and not endpoint_supports_tools("qwen2.5:1.5b-instruct")
      and endpoint_supports_tools("совсем-неизвестная"),
      "supports_tools: qwen — да, ollama — нет, неизвестная — да")

section("3. aggregate(): probe'ы, ошибки, «нет ключа»")
async def _p_ok(base: str) -> list[str]:
    return ["qwen3.8-max", "qwen3-fast"]


async def _p_down(base: str) -> list[str]:
    raise RuntimeError("down")


async def _p_ollama(base: str) -> list[str]:
    return ["qwen2.5:1.5b-instruct", "llama3.2:1b"]


data = asyncio.run(aggregate(probes={
    "qwen": _p_ok, "deepseek": _p_down,
    "ollama_local": _p_ollama, "openai": _p_down,
}))
by_id = {p["id"]: p for p in data["providers"]}
check(by_id["qwen"]["available"] is True
      and by_id["qwen"]["badge"] == "готов",
      "qwen: доступен, бейдж «готов»")
check([m["id"] for m in by_id["qwen"]["models"]]
      == ["qwen3.8-max", "qwen3-fast"],
      "qwen: живой список моделей от probe")
check(by_id["deepseek"]["available"] is False
      and "down" in by_id["deepseek"]["error"],
      "deepseek: недоступен, причина в error")
check(by_id["deepseek"]["models"],
      "deepseek: фолбэк-список из yaml при недоступности")
check(by_id["ollama_local"]["available"] is True
      and by_id["ollama_local"]["badge"] == "работает без интернета",
      "ollama: бейдж «работает без интернета»")
check([m["id"] for m in by_id["ollama_local"]["models"]]
      == ["qwen2.5:1.5b-instruct", "llama3.2:1b"],
      "ollama: установленные модели из /api/tags")

_saved_key = os.environ.pop("DEEPSEEK_API_KEY", None)
try:
    data2 = asyncio.run(aggregate(timeout=0.1))
    by_id2 = {p["id"]: p for p in data2["providers"]}
    check(by_id2["deepseek"]["available"] is False
          and "DEEPSEEK_API_KEY" in by_id2["deepseek"]["error"],
          "deepseek без ключа: «нет ключа DEEPSEEK_API_KEY» без network")
finally:
    if _saved_key is not None:
        os.environ["DEEPSEEK_API_KEY"] = _saved_key

# ═══════════════════════════════════════════════════════════════════
section("4. LLMClient: роутинг + отсечение tools")
from src.llm_client import LLMClient  # noqa: E402

llm = LLMClient(cache=None)
cli_ol, _m1, meta_ol = llm._client_for("qwen2.5:1.5b-instruct")
check(cli_ol is not llm.client, "ollama-модель → отдельный клиент")
check("11434/v1" in str(getattr(cli_ol, "base_url", "")),
      "клиент ollama: base_url …/11434/v1")
check(meta_ol is not None and meta_ol.get("local") is True,
      "meta ollama: local=true")
cli_ol2, _m2, _ = llm._client_for("qwen2.5:1.5b-instruct")
check(cli_ol2 is cli_ol, "клиенты кэшируются (тот же объект)")
cli_unk, _m3, meta_unk = llm._client_for("gpt-4o")
check(cli_unk is llm.client and meta_unk is None,
      "неизвестная модель → дефолтный клиент без meta")

captured: dict = {}


async def _fake_sync(client, messages, tools, tool_choice, model):
    captured["tools"] = tools
    captured["model"] = model
    captured["client"] = client
    return {"role": "assistant", "content": "ok", "chunks": [],
            "streamed": False}


llm._chat_sync = _fake_sync  # type: ignore[method-assign]
TOOLS = [{"type": "function",
          "function": {"name": "t", "parameters": {}}}]
asyncio.run(llm.chat([{"role": "user", "content": "привет"}],
                     tools=TOOLS, model="qwen2.5:1.5b-instruct"))
check(captured["tools"] is None,
      "локальная модель: tools молча убраны")
check(captured["client"] is cli_ol, "локальная модель: запрос уходит в Ollama")
asyncio.run(llm.chat([{"role": "user", "content": "привет"}],
                     tools=TOOLS, model="qwen3.8-max"))
check(captured["tools"] == TOOLS, "облачная модель: tools сохранены")

# ═══════════════════════════════════════════════════════════════════
section("5. ConversationSession: заголовки + messages_view")
from src.supervisor.session import ConversationSession  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    s = ConversationSession(dump_dir=td)
    s.add_user("Проанализируй продажи за квартал в 1С")
    s.add_assistant("Вот анализ…", agent="analyst")
    s.set_title("Анализ продаж 1С")
    s2 = ConversationSession(session_id=s.id, dump_dir=td)
    n = s2.load()
    check(n >= 2 and s2.title == "Анализ продаж 1С"
          and not s2.title_manual,
          "авто-заголовок переживает перезапуск")
    # ручной заголовок: авто после него не перезаписывает
    s2.set_title("Мой важный чат", manual=True)
    s2._dump_raw({"type": "title", "title": "Авто-попытка", "manual": False,
                  "ts": time.time()})
    s3 = ConversationSession(session_id=s.id, dump_dir=td)
    s3.load()
    check(s3.title == "Мой важный чат" and s3.title_manual,
          "ручной заголовок сильнее авто")
    # messages_view
    view = s3.messages_view(10)
    check([v["role"] for v in view] == ["user", "assistant"]
          and view[0]["content"].startswith("Проанализируй"),
          "messages_view: роли и содержимое")

section("6. scan_sessions()")
from src.background.micro_tasks import (  # noqa: E402
    MicroTasksWorker, fallback_title, read_session_file, scan_sessions,
)

with tempfile.TemporaryDirectory() as td:
    td_path = Path(td)
    f1 = td_path / "aaa111.jsonl"
    f1.write_text("\n".join([
        json.dumps({"role": "user", "content": "Проверь бэкапы postgres",
                    "ts": time.time() - 60}),
        json.dumps({"role": "assistant", "content": "Готово", "ts": time.time()}),
        json.dumps({"type": "title", "title": "Проверка бэкапов",
                    "manual": False, "ts": time.time()}),
    ]) + "\n", encoding="utf-8")
    f2 = td_path / "bbb222.jsonl"
    f2.write_text("\n".join([
        json.dumps({"role": "user", "content": "Привет", "ts": time.time()}),
    ]) + "\n", encoding="utf-8")
    (td_path / "empty333.jsonl").write_text("", encoding="utf-8")
    lst = scan_sessions(td_path, limit=10)["sessions"]
    check(len(lst) == 2, "пустые и без-сообщений сессии отброшены")
    titles = {s["id"]: s["title"] for s in lst}
    check(titles.get("aaa111") == "Проверка бэкапов",
          "заголовок из записи title")
    check(titles.get("bbb222") == "", "сессия без заголовка — пустая строка")
    info = read_session_file(f1)
    check(info["msgs"][0]["role"] == "user"
          and info["title"] == "Проверка бэкапов",
          "read_session_file: сообщения + заголовок")
    check(fallback_title("Удалить временные файлы из папки temp")
          .startswith("Удалить временные"),
          "fallback_title: первые слова без модели")

# ═══════════════════════════════════════════════════════════════════
section("7. MicroTasksWorker")


class FakeOllama:
    def __init__(self, reply: str = '{"title": "Тестовый заголовок"}'):
        self.model = "fake:1b"
        self.reply = reply
        self.calls: list[dict] = []

    async def health(self) -> bool:
        return True

    async def chat(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})
        return self.reply


class FakeEvent:
    kind, action, status = "tool_call", "write_file", "ok"
    server_name, agent_id, error = "filesystem", "fs-agent", ""


class FakeStore:
    def query(self, **kw):
        self.kw = kw
        return [FakeEvent()]


class FakeJournal:
    def __init__(self):
        self.store = FakeStore()


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    sess_dir = root / "sessions"
    sess_dir.mkdir()
    f = sess_dir / "ccc333.jsonl"
    f.write_text("\n".join([
        json.dumps({"role": "user",
                    "content": "Сгенерируй отчёт по продажам за март",
                    "ts": time.time() - 120}),
        json.dumps({"role": "assistant", "content": "Отчёт готов…",
                    "ts": time.time() - 100}),
        json.dumps({"role": "user", "content": "Теперь по регионам",
                    "ts": time.time() - 90}),
    ]) + "\n", encoding="utf-8")

    fake = FakeOllama('{"title": "Отчёт по продажам"}')
    w = MicroTasksWorker(
        fake, sess_dir, journal_getter=lambda: FakeJournal(),
        base_dir=root,
    )
    check(w.model == "fake:1b", "модель воркера берётся из ollama.model")
    asyncio.run(w._title_sessions())
    info = read_session_file(f)
    check(info["title"] == "Отчёт по продажам",
          "заголовок чата сгенерирован и дописан в дамп")
    check(fake.calls and fake.calls[0].get("max_tokens") == 32,
          "заголовок: ограничение num_predict")
    check(w.titles_generated == 1, "счётчик заголовков")
    before = len(fake.calls)
    asyncio.run(w._title_sessions())
    check(len(fake.calls) == before, "повторный цикл: дублей нет")
    # ручной заголовок воркер не трогает
    w2 = MicroTasksWorker(fake, sess_dir, base_dir=root)
    f2 = sess_dir / "ddd444.jsonl"
    f2.write_text("\n".join([
        json.dumps({"role": "user", "content": "Вопрос про миграцию базы",
                    "ts": time.time() - 10}),
        json.dumps({"role": "assistant", "content": "Ответ",
                    "ts": time.time()}),
        json.dumps({"type": "title", "title": "Мой чат", "manual": True,
                    "ts": time.time()}),
    ]) + "\n", encoding="utf-8")
    n_calls = len(fake.calls)
    asyncio.run(w2._title_sessions())
    check(len(fake.calls) == n_calls,
          "файл с ручным заголовком воркер пропускает")

    # фолбэк-заголовок при мусорном ответе модели
    f3 = sess_dir / "eee555.jsonl"
    f3.write_text("\n".join([
        json.dumps({"role": "user", "content": "Разобраться с очередью задач",
                    "ts": time.time() - 5}),
        json.dumps({"role": "assistant", "content": "Ок",
                    "ts": time.time()}),
    ]) + "\n", encoding="utf-8")
    bad = FakeOllama("я не json, я просто текст")
    w3 = MicroTasksWorker(bad, sess_dir, base_dir=root)
    asyncio.run(w3._title_sessions())
    info3 = read_session_file(f3)
    check(info3["title"] == fallback_title(
        "Разобраться с очередью задач"),
        "мусорный JSON → детерминированный fallback-заголовок")

    # теги
    tags_fake = FakeOllama('{"tags": ["отчёт", "продажи", "1с"]}')
    w4 = MicroTasksWorker(tags_fake, sess_dir, base_dir=root)
    asyncio.run(w4._tag_sessions())
    info_all = read_session_file(f)
    check(info_all["tags_records"] == 2,
          "теги: по записи на каждое user-сообщение")
    check(w4.tags_generated >= 2,
          "счётчик тегов (включая прочие свежие файлы)")
    st = w4._state["sessions"]["ccc333"]
    check(st["tagged"] == 2, "state: смещение по тегам сохранено")
    n_calls = len(tags_fake.calls)
    asyncio.run(w4._tag_sessions())
    check(len(tags_fake.calls) == n_calls,
          "повторный цикл тегов: новых вызовов нет")

    # дайджест: журнал
    dig_fake = FakeOllama("• Агенты записывали файлы\n• Всё хорошо")
    w5 = MicroTasksWorker(
        dig_fake, sess_dir, journal_getter=lambda: FakeJournal(),
        base_dir=root,
    )
    digest = asyncio.run(w5.force_digest())
    check("•" in digest["text"] and digest["source"] == "журнал"
          and digest["events"] == 1,
          "дайджест: текст от модели, источник «журнал»")
    check((root / "data" / "micro" / "digest.json").exists(),
          "digest.json записан на диск")
    check(w5._state.get("last_digest_ts", 0) > 0,
          "state: last_digest_ts обновлён")

    # дайджест: фолбэк на шину событий
    w6 = MicroTasksWorker(FakeOllama("• пусто"), sess_dir, base_dir=root)
    d6 = asyncio.run(w6.force_digest())
    check(d6["source"] == "шина событий", "дайджест: фолбэк — шина событий")

    # wakeup/stats — безопасны
    w6.wakeup()
    s = w6.stats()
    check(s["model"] == "fake:1b" and "running" in s,
          "stats(): структура на месте")

section("8. RuntimeConfig: model_preferences")
from src.core.runtime_config import RuntimeConfig  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    rc = RuntimeConfig(Path(td) / "runtime.yaml")
    check(rc.get_model_pref() is None, "старт: предпочтение пустое")
    rc.set_model_pref("qwen2.5:1.5b-instruct", written_by="test")
    rc2 = RuntimeConfig(Path(td) / "runtime.yaml")
    check(rc2.get_model_pref() == "qwen2.5:1.5b-instruct",
          "выбор модели переживает перезагрузку конфига")

# ═══════════════════════════════════════════════════════════════════
print(f"\n{'═' * 60}")
print(f"ИТОГО: {_ok}/{_ok + _fail} проверок пройдено")
if _failures:
    print("Провалы:")
    for f_ in _failures:
        print(f"  - {f_}")
sys.exit(1 if _fail else 0)
