"""Анти-zombie клининг портов: поиск и убийство слушателей перед стартом.

Сценарий: qwenproxy/qpx (Node) или прошлый экземпляр агента после
аварийного закрытия окна остаются слушать порт — следующий старт не
может забиндить сокет ([Errno 10048] на Windows / [Errno 98] на Linux).

Модуль находит PID-ы слушателей порта (netstat на Windows, lsof/fuser
на POSIX), при необходимости сверяет командную строку процесса и убивает
чужие зомби. Собственный процесс и его родители никогда не трогаются.

Все системные вызовы инъецируются (run_fn/cmdline_fn/kill_fn) — модуль
тестируется без реальных процессов и платформо-зависимых утилит.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Подстроки командной строки, типичные для зомби нашего же стека.
# Используются как предохранитель: чужие процессы (например, другой
# сервер, случайно севший на порт) под фильтр не попадают — их убийство
# только через явное согласие пользователя.
DEFAULT_MATCH = ("qwenproxy", "qpx", "uvicorn", "run.py", "llm-agent")

# PID-ы, которые нельзя убивать даже если они видны в netstat:
# 0 — System Idle Process, 4 — System (резервирование портов на Windows)
_UNKILLABLE_PIDS = {0, 4}


@dataclass
class PortReport:
    """Итог клининга одного порта."""

    port: int
    found_pids: list[int] = field(default_factory=list)
    killed: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    would_kill: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ═══════════════════════════════════════════════════════════════════════
# Парсеры вывода системных утилит
# ═══════════════════════════════════════════════════════════════════════


def parse_netstat_windows(output: str) -> dict[int, list[int]]:
    """Парсит `netstat -aon` (Windows): TCP ... LISTENING <pid>.

    Возвращает {порт: [pid, ...]} только для LISTENING-строк.
    """
    listeners: dict[int, list[int]] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if parts[3].upper() != "LISTENING":
            continue
        pid_raw = parts[-1]
        if not pid_raw.isdigit():
            continue
        local = parts[1]
        _, _, port_raw = local.rpartition(":")
        if not port_raw.isdigit():
            continue
        pid = int(pid_raw)
        listeners.setdefault(int(port_raw), [])
        if pid not in listeners[int(port_raw)]:
            listeners[int(port_raw)].append(pid)
    return listeners


def parse_lsof_output(output: str) -> dict[int, list[int]]:
    """Парсит `lsof -nP -iTCP -sTCP:LISTEN`: COMMAND PID ... TCP NAME (LISTEN).

    PID — вторая колонка; NAME берётся после колонки NODE («TCP»), т.к.
    lsof дописывает в конец токен «(LISTEN)».
    Возвращает {порт: [pid, ...]}.
    """
    listeners: dict[int, list[int]] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        if parts[0].upper() == "COMMAND":  # заголовок
            continue
        if not parts[1].isdigit() or "TCP" not in parts:
            continue
        pid = int(parts[1])
        node_idx = parts.index("TCP")
        if node_idx + 1 >= len(parts):
            continue
        name_col = parts[node_idx + 1]
        _, _, port_raw = name_col.rpartition(":")
        port_raw = port_raw.split("(")[0].strip()
        if not port_raw.isdigit():
            continue
        listeners.setdefault(int(port_raw), [])
        if pid not in listeners[int(port_raw)]:
            listeners[int(port_raw)].append(pid)
    return listeners


def parse_fuser_output(output: str) -> list[int]:
    """Парсит `fuser -n tcp PORT`: « 123 456» — PID-ы в stdout/stderr."""
    pids: list[int] = []
    for token in output.replace("\n", " ").split():
        if token.isdigit():
            pid = int(token)
            if pid not in pids:
                pids.append(pid)
    return pids


# ═══════════════════════════════════════════════════════════════════════
# Поиск слушателей
# ═══════════════════════════════════════════════════════════════════════


def _default_run(cmd: list[str], timeout: float = 10.0):
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False,
    )


def find_listeners(
    ports: list[int],
    os_name: str | None = None,
    run_fn=_default_run,
) -> dict[int, list[int]]:
    """Возвращает {порт: [pid]} — кто слушает указанные порты.

    Windows: netstat -aon. POSIX: lsof, при его отсутствии — fuser.
    Утилиты не найдены / ошибки — пустой словарь (никогда не падаем:
    клининг не должен ломать старт).
    """
    os_name = os_name or os.name
    wanted = {int(p) for p in ports}
    result: dict[int, list[int]] = {}

    if os_name == "nt":
        try:
            proc = run_fn(["netstat", "-aon"])
            table = parse_netstat_windows(proc.stdout or "")
        except Exception as e:  # noqa: BLE001 — см. докстринг
            logger.warning("netstat недоступен: %s", e)
            return {}
        for port, pids in table.items():
            if port in wanted:
                result[port] = list(pids)
        return result

    # POSIX: сначала lsof (точнее), затем fuser как fallback
    try:
        proc = run_fn(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"])
        if proc.returncode == 0:
            table = parse_lsof_output(proc.stdout or "")
            for port, pids in table.items():
                if port in wanted:
                    result[port] = list(pids)
            if result:
                return result
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.debug("lsof не сработал: %s", e)

    for port in wanted:
        if port in result:
            continue
        try:
            proc = run_fn(["fuser", "-n", "tcp", str(port)])
            pids = [
                p for p in parse_fuser_output(proc.stdout or "")
                if p not in _UNKILLABLE_PIDS
            ]
            pids += [
                p for p in parse_fuser_output(proc.stderr or "")
                if p not in _UNKILLABLE_PIDS
            ]
            if pids:
                result[port] = pids
        except FileNotFoundError:
            break
        except Exception as e:  # noqa: BLE001
            logger.debug("fuser не сработал для %d: %s", port, e)
    return result


# ═══════════════════════════════════════════════════════════════════════
# Командная строка процесса
# ═══════════════════════════════════════════════════════════════════════


def process_cmdline(
    pid: int, os_name: str | None = None, run_fn=_default_run,
) -> str:
    """Командная строка процесса (или имя образа) — для диагностики.

    POSIX: `ps -p PID -o args=`. Windows: wmic, при его отсутствии —
    tasklist (имя образа). Пустая строка — процесс не найден.
    """
    os_name = os_name or os.name
    try:
        if os_name != "nt":
            proc = run_fn(["ps", "-p", str(pid), "-o", "args="])
            if proc.returncode == 0:
                return (proc.stdout or "").strip()
            return ""
        # Windows: wmic выведен из состава Win11 — пробуем, потом tasklist
        proc = run_fn(
            ["wmic", "process", "where", f"processid={pid}", "get",
             "commandline", "/format:list"],
        )
        if proc.returncode == 0:
            for line in (proc.stdout or "").splitlines():
                if line.lower().startswith("commandline="):
                    value = line.split("=", 1)[1].strip()
                    if value:
                        return value
        proc = run_fn(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        )
        out = (proc.stdout or "").strip()
        if out and "INFO:" not in out.upper():
            # CSV: "имя_образа","PID",... — имя образа в колонке 0
            first = out.splitlines()[0]
            name = first.split('","')[0].strip().strip('"')
            if name:
                return name
    except Exception as e:  # noqa: BLE001 — диагностика не должна падать
        logger.debug("process_cmdline(%d) не сработал: %s", pid, e)
    return ""


# ═══════════════════════════════════════════════════════════════════════
# Убийство
# ═══════════════════════════════════════════════════════════════════════


def _kill_default(pid: int, os_name: str | None = None) -> bool:
    """SIGTERM → 1.5с → SIGKILL (POSIX) либо taskkill /F (Windows)."""
    os_name = os_name or os.name
    try:
        if os_name == "nt":
            proc = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if proc.returncode != 0:
                logger.warning("taskkill /PID %d не сработал: %s",
                               pid, (proc.stderr or "").strip())
                return False
            return True
        os.kill(pid, signal.SIGTERM)
        for _ in range(15):  # 1.5с на корректное завершение
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                return True
        os.kill(pid, signal.SIGKILL)
        return True
    except ProcessLookupError:
        # Уже мёртв — считаем успехом (цель — освободить порт)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("kill(%d) не сработал: %s", pid, e)
        return False


# ═══════════════════════════════════════════════════════════════════════
# Оркестрация клининга
# ═══════════════════════════════════════════════════════════════════════


def cleanup_stale_ports(
    ports: list[int],
    *,
    exclude_pids: set[int] | None = None,
    match: tuple[str, ...] | None = DEFAULT_MATCH,
    os_name: str | None = None,
    find_fn=find_listeners,
    cmdline_fn=process_cmdline,
    kill_fn=_kill_default,
    dry_run: bool = False,
) -> list[PortReport]:
    """Убивает зомби, слушающие указанные порты. Возвращает отчёты.

    exclude_pids — PID-ы, которые не трогать (свой процесс, родители).
    match — фильтр по подстрокам командной строки (None = убить любого
    слушателя, кроме исключённых; по умолчанию — только наш стек).
    dry_run — только отчёт, без убийства.
    """
    os_name = os_name or os.name
    exclude = set(exclude_pids or set()) | _UNKILLABLE_PIDS
    listeners = find_fn(list(ports), os_name=os_name)

    reports: list[PortReport] = []
    for port in ports:
        report = PortReport(port=int(port))
        pids = listeners.get(int(port), [])
        report.found_pids = list(pids)
        for pid in pids:
            if pid in exclude or pid == os.getpid():
                report.skipped.append(pid)
                continue
            if match is not None:
                cmdline = cmdline_fn(pid, os_name=os_name)
                low = cmdline.lower()
                if not any(m.lower() in low for m in match if m):
                    report.skipped.append(pid)
                    logger.info(
                        "Порт %d держит PID %d (%r) — не похож на наш "
                        "стек, не трогаю", port, pid, cmdline[:80],
                    )
                    continue
            if dry_run:
                report.would_kill.append(pid)
                continue
            if kill_fn(pid):
                report.killed.append(pid)
            else:
                report.errors.append(f"kill {pid} failed")
        reports.append(report)
    return reports
