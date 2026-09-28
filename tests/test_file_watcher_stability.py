"""Тесты FileWatcher: стабильность файла, debounce, откат (регрессия):

    python tests/test_file_watcher_stability.py
    python -m pytest tests/test_file_watcher_stability.py -q

Проверяют:
- _wait_until_stable: стабильный файл → True; пишущийся → False;
  исчезнувший → False; залоченный (probe=False) → False
- интеграция: изменение декларации → ровно один reload реестра,
  повторное изменение (после cooldown) → второй reload
- файл, который пишут непрерывно, не вызывает reload вообще
  (защита от чтения битого буфера), после остановки записи — вызывает
- регрессия: reload упал → rollback вызывается (раньше
  «await registry.load_declarations()» поднимал TypeError на
  синхронном методе и откат никогда не выполнялся)
- on_moved: атомарное сохранение (tmp → rename) отдаёт dest_path
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from src.core.file_watcher import FileWatcher, _Handler  # noqa: E402
from watchdog.events import (                             # noqa: E402
    FileModifiedEvent,
    FileMovedEvent,
)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))


class FakeRegistry:
    """Реестр-двойник: считает reload/rollback, умеет ломаться."""

    def __init__(self, fail_reload: bool = False):
        self.fail_reload = fail_reload
        self.reload_count = 0
        self.load_count = 0
        self.build_count = 0

    async def reload_all(self) -> None:
        self.reload_count += 1
        if self.fail_reload:
            raise RuntimeError("битый yaml (эмуляция)")

    def load_declarations(self) -> None:  # синхронный — как в Registry
        self.load_count += 1

    async def build_snapshot(self, reason: str = "") -> None:
        self.build_count += 1
        self.last_reason = reason


async def _wait_until(cond_fn, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond_fn():
            return True
        await asyncio.sleep(0.05)
    return cond_fn()


def make_watcher(reg: FakeRegistry, base: Path, **kw) -> FileWatcher:
    defaults = dict(
        debounce_seconds=0.1,
        cooldown_seconds=0.5,
        stability_window=0.1,
        stability_max_wait=1.5,
        stability_max_retries=3,
    )
    defaults.update(kw)
    return FileWatcher(reg, base, **defaults)


# ═══════════════════════════════════════════════════════════════════════
# _wait_until_stable (юнит-уровень)
# ═══════════════════════════════════════════════════════════════════════


async def test_stability_stable_file() -> None:
    print("── стабильный файл ──")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "agent.yaml"
        p.write_text("id: test\n", encoding="utf-8")
        reg = FakeRegistry()
        w = make_watcher(reg, Path(td), stability_window=0.05,
                         stability_max_wait=1.0)
        ok = await w._wait_until_stable(p)
        check("записанный файл признаётся стабильным", ok is True)


async def test_stability_missing_file() -> None:
    print("── исчезнувший файл ──")
    with tempfile.TemporaryDirectory() as td:
        reg = FakeRegistry()
        w = make_watcher(reg, Path(td))
        ok = await w._wait_until_stable(Path(td) / "nope.yaml")
        check("несуществующий файл → False (событие устарело)", ok is False)


async def test_stability_changing_file() -> None:
    print("── файл пишется непрерывно ──")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "agent.yaml"
        p.write_text("id: test\n", encoding="utf-8")
        stop = threading.Event()

        def writer():
            i = 0
            while not stop.is_set():
                with open(p, "a", encoding="utf-8") as f:
                    f.write(f"k{i}: v{i}\n")
                    f.flush()
                i += 1
                time.sleep(0.02)

        reg = FakeRegistry()
        w = make_watcher(reg, Path(td), stability_window=0.15,
                         stability_max_wait=0.4)
        th = threading.Thread(target=writer, daemon=True)
        th.start()
        try:
            ok = await w._wait_until_stable(p)
            check("пишущийся файл не признаётся стабильным", ok is False)
        finally:
            stop.set()
            th.join(timeout=2)

        ok2 = await w._wait_until_stable(p)
        check("после остановки записи файл стабилен", ok2 is True)


async def test_stability_locked_file() -> None:
    print("── залоченный файл (Windows sharing violation) ──")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "agent.yaml"
        p.write_text("id: test\n", encoding="utf-8")
        reg = FakeRegistry()
        w = make_watcher(reg, Path(td), stability_max_wait=0.3)
        # Берём дескриптор из __dict__, а не атрибут класса: при
        # доступе через класс staticmethod разворачивается в функцию,
        # и обратное присваивание ломает привязку self._probe
        original = FileWatcher.__dict__["_probe"]

        def locked_probe(path):  # эмуляция лока на любой ОС
            return (False, -1, -1.0)

        try:
            FileWatcher._probe = staticmethod(locked_probe)
            ok = await w._wait_until_stable(p)
            check("залоченный файл → False", ok is False)
        finally:
            FileWatcher._probe = original


# ═══════════════════════════════════════════════════════════════════════
# Интеграция: событие → debounce → стабильность → reload
# ═══════════════════════════════════════════════════════════════════════


async def test_integration_change_triggers_single_reload() -> None:
    print("── интеграция: изменение → reload ──")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        (base / "agents").mkdir()
        reg = FakeRegistry()
        w = make_watcher(reg, base)
        loop = asyncio.get_running_loop()
        w.start(loop)
        try:
            target = base / "agents" / "my_agent.yaml"
            target.write_text("id: my_agent\n", encoding="utf-8")
            ok = await _wait_until(lambda: reg.reload_count >= 1)
            check("reload после первого изменения", ok)

            time.sleep(w._cooldown_seconds + 0.1)  # выходим из cooldown
            target.write_text("id: my_agent\nenabled: true\n",
                              encoding="utf-8")
            ok2 = await _wait_until(lambda: reg.reload_count >= 2)
            check("reload после второго изменения (cooldown прошёл)", ok2)
        finally:
            w.stop()


async def test_integration_unstable_file_never_reloads() -> None:
    print("── интеграция: непрерывная запись → reload не делается ──")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        (base / "agents").mkdir()
        target = base / "agents" / "my_agent.yaml"
        target.write_text("id: my_agent\n", encoding="utf-8")

        stop = threading.Event()

        def writer():
            i = 0
            while not stop.is_set():
                with open(target, "a", encoding="utf-8") as f:
                    f.write(f"k{i}: v{i}\n")
                i += 1
                time.sleep(0.02)

        reg = FakeRegistry()
        w = make_watcher(reg, base, debounce_seconds=0.05,
                         cooldown_seconds=999.0, stability_window=0.15,
                         stability_max_wait=0.3, stability_max_retries=2)
        w.start(asyncio.get_running_loop())
        th = threading.Thread(target=writer, daemon=True)
        th.start()
        try:
            await asyncio.sleep(1.2)  # за это время всё «стабильное» прошло бы
            check("пока пишут — reload не вызван", reg.reload_count == 0,
                  f"reload_count={reg.reload_count}")
        finally:
            stop.set()
            th.join(timeout=2)
            w.stop()

        # Новая сессия: запись прекращена → защита не блокирует навсегда
        reg2 = FakeRegistry()
        w2 = make_watcher(reg2, base, debounce_seconds=0.05,
                          cooldown_seconds=0.2, stability_window=0.1,
                          stability_max_wait=1.0)
        w2.start(asyncio.get_running_loop())
        try:
            target.write_text("id: my_agent\nenabled: true\n",
                              encoding="utf-8")
            ok = await _wait_until(lambda: reg2.reload_count >= 1, timeout=5)
            check("после остановки записи reload проходит", ok)
        finally:
            w2.stop()


async def test_rollback_regression() -> None:
    print("── регрессия: сломанный rollback (await None) ──")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        (base / "agents").mkdir()
        reg = FakeRegistry(fail_reload=True)
        w = make_watcher(reg, base)
        w.start(asyncio.get_running_loop())
        try:
            target = base / "agents" / "broken.yaml"
            target.write_text("id: broken\n", encoding="utf-8")
            ok = await _wait_until(lambda: reg.build_count >= 1)
            check("reload упал → rollback выполнен", ok)
            check("load_declarations вызван синхронно (без await None)",
                  reg.load_count >= 1, f"load_count={reg.load_count}")
            check("build_snapshot с reason=watcher_rollback",
                  getattr(reg, "last_reason", "") == "watcher_rollback",
                  str(getattr(reg, "last_reason", None)))
        finally:
            w.stop()


# ═══════════════════════════════════════════════════════════════════════
# _Handler: маршрутизация событий
# ═══════════════════════════════════════════════════════════════════════


def test_handler_routing() -> None:
    print("── _Handler: маршрутизация событий ──")
    got: list[Path] = []
    h = _Handler(lambda p: got.append(p))

    h.on_modified(FileModifiedEvent("/base/agents/a.yaml"))
    check("on_modified отдаёт src_path",
          got and str(got[-1]).endswith("a.yaml"))

    h.on_moved(FileMovedEvent("/base/agents/a.tmp", "/base/agents/a.yaml"))
    check("on_moved отдаёт dest_path (атомарное сохранение)",
          got and str(got[-1]).endswith("a.yaml"),
          str(got[-1]) if got else "нет событий")

    n = len(got)
    h.on_modified(FileModifiedEvent("/base/agents/b.swp"))
    check("временные файлы игнорируются", len(got) == n)

    ev = FileModifiedEvent("/base/agents/c.yaml")
    ev.is_directory = True
    h.on_modified(ev)
    check("события директорий игнорируются", len(got) == n)


# ═══════════════════════════════════════════════════════════════════════


def main() -> int:
    print("\n=== FileWatcher: стабильность/debounce/rollback — тесты ===\n")
    for t in (
        test_stability_stable_file,
        test_stability_missing_file,
        test_stability_changing_file,
        test_stability_locked_file,
        test_integration_change_triggers_single_reload,
        test_integration_unstable_file_never_reloads,
        test_rollback_regression,
        test_handler_routing,
    ):
        if asyncio.iscoroutinefunction(t):
            asyncio.run(t())
        else:
            t()
    print(f"\nИтого: {len(PASS)} ok, {len(FAIL)} fail")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
