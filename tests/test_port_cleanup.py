"""Тесты анти-zombie клининга портов (src/port_cleanup.py):

    python tests/test_port_cleanup.py
    python -m pytest tests/test_port_cleanup.py -q

Реальных процессов и платформенных утилит почти не требуют: парсеры
проверяются на образцах вывода netstat/lsof/fuser, оркестрация — на
подменных find/cmdline/kill. Один интеграционный тест связывает
реальный сокет и ищет его через lsof (пропускается, если lsof нет).
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from src.port_cleanup import (            # noqa: E402
    DEFAULT_MATCH,
    PortReport,
    cleanup_stale_ports,
    find_listeners,
    parse_fuser_output,
    parse_lsof_output,
    parse_netstat_windows,
    process_cmdline,
)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))


class RunResult:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


# ═══════════════════════════════════════════════════════════════════════
# Парсеры
# ═══════════════════════════════════════════════════════════════════════


def test_parse_netstat_windows() -> None:
    print("── parse_netstat_windows ──")
    sample = """
  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:7936           0.0.0.0:0              LISTENING       12345
  TCP    0.0.0.0:7936           0.0.0.0:0              LISTENING       12345
  TCP    127.0.0.1:8000         0.0.0.0:0              LISTENING       999
  TCP    [::]:49666             [::]:0                 LISTENING       4
  TCP    127.0.0.1:8000         127.0.0.1:51234        ESTABLISHED     999
  TCP    127.0.0.1:51235        127.0.0.1:8000         TIME_WAIT       0
  UDP    0.0.0.0:5353           *:*                                    111
"""
    got = parse_netstat_windows(sample)
    check("только LISTENING-строки",
          got == {7936: [12345], 8000: [999], 49666: [4]}, str(got))
    check("дубликаты PID схлопнуты", got.get(7936) == [12345])
    check("TIME_WAIT/UDP отсечены", 5353 not in got and 51235 not in got)


def test_parse_lsof_output() -> None:
    print("── parse_lsof_output ──")
    sample = (
        "COMMAND   PID  USER   FD   TYPE  DEVICE SIZE/OFF NODE NAME\n"
        "node    41021  user   23u  IPv4  0xabcd      0t0  TCP "
        "127.0.0.1:7936 (LISTEN)\n"
        "python  41022  user   19u  IPv6  0xabce      0t0  TCP "
        "*:8000 (LISTEN)\n"
    )
    got = parse_lsof_output(sample)
    check("порт → pid из lsof", got == {7936: [41021], 8000: [41022]}, str(got))


def test_parse_fuser_output() -> None:
    print("── parse_fuser_output ──")
    got = parse_fuser_output(" 123 456\n 123\n")
    check("pid-ы из fuser + дедупликация", got == [123, 456], str(got))


# ═══════════════════════════════════════════════════════════════════════
# find_listeners (подменный run_fn, включая Windows-ветку)
# ═══════════════════════════════════════════════════════════════════════


def test_find_listeners_windows_via_fake_run() -> None:
    print("── find_listeners (nt, подменный run_fn) ──")
    netstat_out = (
        "  TCP    0.0.0.0:7936    0.0.0.0:0    LISTENING    4242\n"
        "  TCP    127.0.0.1:8000   0.0.0.0:0    LISTENING    555\n"
    )
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=10.0):
        calls.append(cmd)
        return RunResult(stdout=netstat_out)

    got = find_listeners([7936, 8000], os_name="nt", run_fn=fake_run)
    check("netstat-ветка отдаёт pid-ы",
          got == {7936: [4242], 8000: [555]}, str(got))
    check("вызван именно netstat", calls and calls[0][:1] == ["netstat"])


def test_find_listeners_lsof_fallback_fuser() -> None:
    print("── find_listeners (posix: lsof → fuser) ──")

    def run_lsof_fail(cmd, timeout=10.0):
        if cmd[0] == "lsof":
            return RunResult(returncode=1)  # lsof ничего не нашёл/ошибка
        # fuser -n tcp PORT
        return RunResult(stdout=" 777\n", returncode=0)

    got = find_listeners([7940], os_name="posix", run_fn=run_lsof_fail)
    check("fallback на fuser", got == {7940: [777]}, str(got))

    def run_no_tools(cmd, timeout=10.0):
        raise FileNotFoundError(cmd[0])

    got2 = find_listeners([7940], os_name="posix", run_fn=run_no_tools)
    check("утилит нет → пусто, без падения", got2 == {}, str(got2))


def test_find_listeners_real_socket() -> None:
    print("── find_listeners (реальный сокет) ──")
    if os.name != "posix" or shutil.which("lsof") is None:
        check("пропущено (нет lsof/не posix)", True)
        return
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        got = find_listeners([port], os_name="posix")
        check("наш собственный слушатель найден",
              got.get(port) == [os.getpid()], str(got))


# ═══════════════════════════════════════════════════════════════════════
# process_cmdline
# ═══════════════════════════════════════════════════════════════════════


def test_process_cmdline_posix_real_self() -> None:
    print("── process_cmdline (posix, свой pid) ──")
    if os.name != "posix":
        check("пропущено (не posix)", True)
        return
    got = process_cmdline(os.getpid(), os_name="posix")
    check("свою командную строку видим", "python" in got.lower(), got[:60])


def test_process_cmdline_windows_paths() -> None:
    print("── process_cmdline (nt: wmic → tasklist) ──")

    def run_wmic_ok(cmd, timeout=10.0):
        if cmd[0] == "wmic":
            return RunResult(
                stdout="\r\nCommandLine=C:\\qwenproxy\\cli.js --port 7936\r\n\r\n",
            )
        return RunResult(returncode=1)

    got = process_cmdline(4242, os_name="nt", run_fn=run_wmic_ok)
    check("wmic CommandLine распарсен",
          got == "C:\\qwenproxy\\cli.js --port 7936", got)

    def run_wmic_dead(cmd, timeout=10.0):
        if cmd[0] == "wmic":
            return RunResult(returncode=1)  # wmic нет (Win11)
        return RunResult(
            stdout='"node.exe","4242","Console","1","10 000 K"\n')

    got2 = process_cmdline(4242, os_name="nt", run_fn=run_wmic_dead)
    check("tasklist fallback даёт имя образа", got2 == "node.exe", got2)

    def run_info(cmd, timeout=10.0):
        return RunResult(stdout="INFO: No tasks are running...\n")

    got3 = process_cmdline(4242, os_name="nt", run_fn=run_info)
    check("нет процесса → пусто", got3 == "", got3)


# ═══════════════════════════════════════════════════════════════════════
# cleanup_stale_ports — оркестрация на подменных функциях
# ═══════════════════════════════════════════════════════════════════════


def test_cleanup_match_and_exclusions() -> None:
    print("── cleanup_stale_ports (match/exclude) ──")
    find_fake = lambda ports, os_name=None: {7936: [111, 222, 333]}  # noqa: E731
    cmdlines = {
        111: "node C:\\app\\qwenproxy\\cli.js --port 7936",
        222: "node C:\\app\\qwenproxy\\cli.js --port 7936",  # свой → exclude
        333: "C:\\Program Files\\Mozilla Firefox\\firefox.exe",
    }
    killed: list[int] = []

    def kill_fake(pid):
        killed.append(pid)
        return True

    reports = cleanup_stale_ports(
        [7936],
        exclude_pids={222},
        match=DEFAULT_MATCH,
        find_fn=find_fake,
        cmdline_fn=lambda pid, os_name=None: cmdlines[pid],
        kill_fn=kill_fake,
        os_name="nt",
    )
    rep = reports[0]
    check("зомби нашего стека убит", rep.killed == [111], str(rep))
    check("чужой процесс пропущен (не наш стек)",
          rep.skipped == [222, 333], str(rep.skipped))
    check("kill вызван ровно один раз", killed == [111])
    check("отчёт без ошибок", rep.ok)


def test_cleanup_dry_run_and_failures() -> None:
    print("── cleanup_stale_ports (dry_run / ошибки kill) ──")
    find_fake = lambda ports, os_name=None: {8000: [555]}  # noqa: E731
    calls: list[int] = []

    def kill_fake(pid):
        calls.append(pid)
        return False  # kill всегда «не срабатывает»

    rep = cleanup_stale_ports(
        [8000], match=None, find_fn=find_fake, kill_fn=kill_fake,
        dry_run=True, os_name="posix",
    )[0]
    check("dry_run: только would_kill", rep.would_kill == [555], str(rep))
    check("dry_run: kill не вызывался", calls == [])
    check("dry_run: killed пуст", rep.killed == [])

    rep2 = cleanup_stale_ports(
        [8000], match=None, find_fn=find_fake, kill_fn=kill_fake,
        dry_run=False, os_name="posix",
    )[0]
    check("ошибка kill попадает в отчёт", not rep2.ok
          and rep2.errors == ["kill 555 failed"], str(rep2))


def test_cleanup_own_pid_never_killed() -> None:
    print("── cleanup_stale_ports (свой PID в списке) ──")
    my_pid = os.getpid()
    find_fake = lambda ports, os_name=None: {8000: [my_pid]}  # noqa: E731
    rep = cleanup_stale_ports(
        [8000], match=None, find_fn=find_fake,
        cmdline_fn=lambda pid, os_name=None: "python run.py",
        kill_fn=lambda pid: True,
        os_name="posix",
    )[0]
    check("собственный процесс не тронут",
          rep.skipped == [my_pid] and rep.killed == [], str(rep))


def test_cleanup_windows_system_pids_skipped() -> None:
    print("── cleanup_stale_ports (System PID 0/4) ──")
    find_fake = lambda ports, os_name=None: {49666: [4]}  # noqa: E731
    rep = cleanup_stale_ports(
        [49666], match=None, find_fn=find_fake,
        kill_fn=lambda pid: True, os_name="nt",
    )[0]
    check("PID 4 (System) не отправлен на убийство",
          rep.killed == [] and 4 in rep.skipped, str(rep))


def test_report_defaults() -> None:
    print("── PortReport ──")
    rep = PortReport(port=1)
    check("пустой отчёт ok", rep.ok)
    rep.errors.append("x")
    check("с ошибкой not ok", not rep.ok)


# ═══════════════════════════════════════════════════════════════════════


def main() -> int:
    print("\n=== Анти-zombie клининг портов: тесты ===\n")
    for t in (
        test_parse_netstat_windows,
        test_parse_lsof_output,
        test_parse_fuser_output,
        test_find_listeners_windows_via_fake_run,
        test_find_listeners_lsof_fallback_fuser,
        test_find_listeners_real_socket,
        test_process_cmdline_posix_real_self,
        test_process_cmdline_windows_paths,
        test_cleanup_match_and_exclusions,
        test_cleanup_dry_run_and_failures,
        test_cleanup_own_pid_never_killed,
        test_cleanup_windows_system_pids_skipped,
        test_report_defaults,
    ):
        t()
    print(f"\nИтого: {len(PASS)} ok, {len(FAIL)} fail")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
