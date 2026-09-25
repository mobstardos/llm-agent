"""YAxUnit runner — unit-тесты 1С.

YAxUnit запускается через 1cv8.exe ENTERPRISE с параметром /C.
Пример:
  1cv8.exe ENTERPRISE /F<ib> /N<user> /P<pass> /C "RunUnitTests=ПутьКМодулю"
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from src.mcp_servers.onec_designer.finder import find_1cv8_enterprise

logger = logging.getLogger(__name__)


@dataclass
class YAxUnitResult:
    success: bool = False
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    duration_ms: float = 0.0
    details: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    report_path: str = ""


class YAxUnitRunner:
    def __init__(
        self,
        ib_path: str | None = None,
        user: str | None = None,
        password: str | None = None,
        timeout_seconds: int = 600,
    ):
        self.ib_path = ib_path or ""
        self.user = user or ""
        self.password = password or ""
        self.timeout_seconds = timeout_seconds
        self.binary = find_1cv8_enterprise()

    def available(self) -> bool:
        return self.binary is not None and self.binary.exists()

    async def run(
        self,
        module_filter: str = "",
        report_path: str | Path | None = None,
    ) -> YAxUnitResult:
        """Запускает YAxUnit-тесты."""
        if not self.available():
            return YAxUnitResult(
                errors=["1cv8c.exe (тонкий клиент) не найден"],
            )
        if not self.ib_path:
            return YAxUnitResult(errors=["ONEC_IB_PATH не задан"])

        report_file = Path(report_path) if report_path else (
            Path("data") / f"yaxunit_{int(time.time())}.json"
        )
        report_file.parent.mkdir(parents=True, exist_ok=True)

        # Формируем /C
        params = [
            f"RunUnitTests=YAxUnit",
            f"ReportPath={report_file}",
        ]
        if module_filter:
            params.append(f"Filter={module_filter}")
        c_params = ";".join(params)

        args = [
            str(self.binary),
            "ENTERPRISE",
        ]
        ib = self.ib_path
        if ib.startswith("File=") or ib.startswith("Srvr="):
            args.append(ib)
        else:
            args.append(f"/F{ib}")
        if self.user:
            args.append(f"/N{self.user}")
        if self.password:
            args.append(f"/P{self.password}")
        args.extend(["/C", c_params])

        t0 = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout_seconds,
                )
            except asyncio.TimeoutError:
                proc.kill()
                return YAxUnitResult(
                    errors=[f"Timeout {self.timeout_seconds}s"],
                    duration_ms=(time.perf_counter() - t0) * 1000,
                )
        except FileNotFoundError:
            return YAxUnitResult(errors=["1cv8c.exe не найден"])

        duration_ms = (time.perf_counter() - t0) * 1000

        # Парсим отчёт
        result = self._parse_report(report_file)
        result.duration_ms = duration_ms
        result.report_path = str(report_file)

        return result

    @staticmethod
    def _parse_report(report_path: Path) -> YAxUnitResult:
        result = YAxUnitResult(report_path=str(report_path))
        if not report_path.exists():
            result.errors.append(f"Отчёт не найден: {report_path}")
            return result

        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as e:
            result.errors.append(f"Парсинг отчёта: {e}")
            return result

        # YAxUnit формат (примерный)
        result.total = int(data.get("total", 0) or 0)
        result.passed = int(data.get("passed", 0) or 0)
        result.failed = int(data.get("failed", 0) or 0)
        result.skipped = int(data.get("skipped", 0) or 0)

        for t in (data.get("tests") or []):
            result.details.append({
                "name": t.get("name", ""),
                "status": t.get("status", ""),
                "duration_ms": t.get("duration_ms", 0),
                "error": t.get("error", ""),
            })

        result.success = result.failed == 0 and result.errors == []
        return result
