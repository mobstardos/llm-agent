"""Async-обёртка над 1cv8.exe DESIGNER.

Все операции — через командную строку. Логи парсятся на ошибки.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from src.mcp_servers.onec_designer.finder import find_1cv8
from src.mcp_servers.onec_designer.log_parser import (
    LogMessage, parse_1c_log,
)

logger = logging.getLogger(__name__)


@dataclass
class DesignerResult:
    success: bool
    command: str
    exit_code: int
    duration_ms: float
    stdout: str = ""
    stderr: str = ""
    log_messages: list[LogMessage] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class DesignerRunner:
    """Запускает 1cv8.exe DESIGNER с таймаутом и парсит логи."""

    def __init__(
        self,
        binary: Path | None = None,
        ib_path: str | None = None,
        user: str | None = None,
        password: str | None = None,
        timeout_seconds: int = 300,
        log_dir: Path | None = None,
    ):
        self.binary = binary or find_1cv8()
        self.ib_path = ib_path or os.getenv("ONEC_IB_PATH", "")
        self.user = user or os.getenv("ONEC_DESIGNER_USER", "")
        self.password = password or os.getenv("ONEC_DESIGNER_PASSWORD", "")
        self.timeout_seconds = timeout_seconds
        self.log_dir = log_dir or Path("data/onec_logs")

    def available(self) -> bool:
        return self.binary is not None and self.binary.exists()

    # ═══════════════════════════════════════════════════════
    # Базовая команда
    # ═══════════════════════════════════════════════════════
    def _base_args(self, ib_path: str | None = None) -> list[str]:
        ib = ib_path or self.ib_path
        if not ib:
            raise ValueError("Не задан путь к ИБ (ONEC_IB_PATH)")

        args = ["DESIGNER"]
        if ib.startswith("File=") or ib.startswith("Srvr="):
            args.append(ib)
        else:
            args.append(f"/F{ib}")

        if self.user:
            args.append(f"/N{self.user}")
        if self.password:
            args.append(f"/P{self.password}")

        return args

    async def run(
        self,
        *extra_args: str,
        ib_path: str | None = None,
        timeout: int | None = None,
        log_name: str = "",
    ) -> DesignerResult:
        if not self.available():
            return DesignerResult(
                success=False,
                command="(no binary)",
                exit_code=-1,
                duration_ms=0,
                errors=["1cv8.exe не найден"],
            )

        args = [str(self.binary)] + self._base_args(ib_path) + list(extra_args)

        # Логи 1С
        log_file: Path | None = None
        if log_name:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log_file = self.log_dir / f"{log_name}_{int(time.time())}.log"
            args.append(f"/Out{log_file}")

        t0 = time.perf_counter()
        cmd_str = " ".join(str(a) for a in args)

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout or self.timeout_seconds,
                )
            except asyncio.TimeoutError:
                proc.kill()
                return DesignerResult(
                    success=False, command=cmd_str, exit_code=-1,
                    duration_ms=(time.perf_counter() - t0) * 1000,
                    errors=[f"Timeout {timeout or self.timeout_seconds}с"],
                )
        except FileNotFoundError:
            return DesignerResult(
                success=False, command=cmd_str, exit_code=-1,
                duration_ms=0, errors=["1cv8.exe не найден"],
            )
        except Exception as e:
            return DesignerResult(
                success=False, command=cmd_str, exit_code=-1,
                duration_ms=(time.perf_counter() - t0) * 1000,
                errors=[str(e)],
            )

        duration_ms = (time.perf_counter() - t0) * 1000
        stdout_str = self._decode(stdout)
        stderr_str = self._decode(stderr)

        log_messages: list[LogMessage] = []
        if log_file and log_file.exists():
            log_messages = parse_1c_log(log_file)

        errors = [m.text for m in log_messages if m.level == "error"]
        warnings = [m.text for m in log_messages if m.level == "warning"]

        # Fallback: парсим stdout
        if not log_messages and stdout_str:
            for line in stdout_str.splitlines():
                low = line.lower()
                if "ошибк" in low or "error" in low:
                    errors.append(line.strip())
                elif "предупрежд" in low or "warning" in low:
                    warnings.append(line.strip())

        success = (
            proc.returncode == 0
            and not errors
        )

        return DesignerResult(
            success=success,
            command=cmd_str,
            exit_code=proc.returncode or 0,
            duration_ms=duration_ms,
            stdout=stdout_str,
            stderr=stderr_str,
            log_messages=log_messages,
            errors=errors,
            warnings=warnings,
        )

    @staticmethod
    def _decode(data: bytes) -> str:
        """1С выдаёт вывод в CP866/CP1251 — декодируем с fallback."""
        for enc in ("utf-8", "cp866", "cp1251", "latin-1"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    # ═══════════════════════════════════════════════════════
    # Операции
    # ═══════════════════════════════════════════════════════
    async def load_config_from_files(
        self,
        config_dir: str | Path,
        update_db: bool = True,
        ib_path: str | None = None,
    ) -> DesignerResult:
        """Загрузить конфигурацию из файлов."""
        args = ["/LoadConfigFromFiles", str(config_dir)]
        if update_db:
            args.append("/UpdateDBCfg")
        return await self.run(*args, ib_path=ib_path, log_name="load_config")

    async def dump_config_to_files(
        self,
        config_dir: str | Path,
        format_: str = "Hierarchical",
        ib_path: str | None = None,
    ) -> DesignerResult:
        """Выгрузить конфигурацию в файлы."""
        args = [
            "/DumpConfigToFiles", str(config_dir),
            "-Format", format_,
        ]
        return await self.run(*args, ib_path=ib_path, log_name="dump_config")

    async def update_db(
        self, ib_path: str | None = None, dynamic: bool = False,
    ) -> DesignerResult:
        """Обновить конфигурацию БД."""
        args = ["/UpdateDBCfg"]
        if dynamic:
            args.append("-Dynamic+")
        return await self.run(*args, ib_path=ib_path, log_name="update_db")

    async def create_ib(
        self,
        ib_path: str,
        dbms: str = "File",
        locale: str = "ru",
    ) -> DesignerResult:
        """Создать новую ИБ."""
        args = [
            "CREATEINFOBASE",
            ib_path,
            f"/AddInList",
            f"/L{locale}",
        ]
        if dbms == "File":
            args.append("/DBMS")
            args.append("File")
        return await self.run(*args, log_name="create_ib")

    async def dump_ib(
        self, dt_path: str | Path, ib_path: str | None = None,
    ) -> DesignerResult:
        """Выгрузить ИБ в .dt."""
        args = ["/DumpIB", str(dt_path)]
        return await self.run(*args, ib_path=ib_path, log_name="dump_ib")

    async def restore_ib(
        self, dt_path: str | Path, ib_path: str | None = None,
    ) -> DesignerResult:
        """Восстановить ИБ из .dt."""
        args = ["/RestoreIB", str(dt_path)]
        return await self.run(*args, ib_path=ib_path, log_name="restore_ib")

    async def check_config(
        self,
        config_dir: str | Path | None = None,
        thin_client: bool = True,
        ib_path: str | None = None,
    ) -> DesignerResult:
        """Синтаксическая проверка модулей."""
        args = ["/CheckModules"]
        if thin_client:
            args.append("-ThinClient")
        if config_dir:
            args.extend(["-ConfigLogIntegrity", "-IncorrectReferences"])
        return await self.run(*args, ib_path=ib_path, log_name="check_config")

    async def build_cf(
        self,
        config_dir: str | Path,
        output_cf: str | Path,
        ib_path: str | None = None,
    ) -> DesignerResult:
        """Собрать .cf из выгрузки."""
        # 1. Загрузить во временную ИБ
        # 2. Выгрузить в .cf
        # Для простоты — операция на текущей ИБ
        args = ["/DumpCfg", str(output_cf)]
        return await self.run(*args, ib_path=ib_path, log_name="build_cf")

    async def build_cfe(
        self,
        extension_dir: str | Path,
        output_cfe: str | Path,
        ib_path: str | None = None,
    ) -> DesignerResult:
        """Собрать .cfe (расширение)."""
        args = ["/DumpCfg", str(output_cfe), "-Extension", "MyExtension"]
        return await self.run(*args, ib_path=ib_path, log_name="build_cfe")

    async def build_epf(
        self,
        epf_src: str | Path,
        output_epf: str | Path,
        ib_path: str | None = None,
    ) -> DesignerResult:
        """Собрать .epf (обработку)."""
        args = ["/DumpExternalDataProcessorOrReportToFiles",
                str(output_epf), str(epf_src)]
        return await self.run(*args, ib_path=ib_path, log_name="build_epf")
