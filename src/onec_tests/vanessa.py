"""Vanessa Automation runner — BDD-тесты 1С.

Запускает feature-файлы через 1cv8.exe ENTERPRISE с внешней обработкой
Vanessa-Automation.epf.
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
class VanessaResult:
    success: bool = False
    scenarios_total: int = 0
    scenarios_passed: int = 0
    scenarios_failed: int = 0
    steps_total: int = 0
    steps_passed: int = 0
    steps_failed: int = 0
    duration_ms: float = 0.0
    details: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    report_path: str = ""


class VanessaRunner:
    def __init__(
        self,
        ib_path: str | None = None,
        vanessa_epf: str | None = None,
        user: str | None = None,
        password: str | None = None,
        timeout_seconds: int = 1800,
    ):
        self.ib_path = ib_path or ""
        self.vanessa_epf = vanessa_epf or ""
        self.user = user or ""
        self.password = password or ""
        self.timeout_seconds = timeout_seconds
        self.binary = find_1cv8_enterprise()

    def available(self) -> bool:
        return (
            self.binary is not None
            and self.binary.exists()
            and bool(self.vanessa_epf)
            and Path(self.vanessa_epf).exists()
        )

    async def run_features(
        self,
        features_dir: str | Path,
        report_path: str | Path | None = None,
        filter_: str = "",
    ) -> VanessaResult:
        if not self.available():
            return VanessaResult(
                errors=[
                    "1cv8c.exe не найден или Vanessa-Automation.epf не "
                    "указан в ONEC_VANESSA_EPF",
                ],
            )

        features_dir = Path(features_dir)
        if not features_dir.exists():
            return VanessaResult(
                errors=[f"Директория features не найдена: {features_dir}"],
            )

        report_file = Path(report_path) if report_path else (
            Path("data") / f"vanessa_{int(time.time())}.json"
        )
        report_file.parent.mkdir(parents=True, exist_ok=True)

        # Формируем /C
        params = [
            "vanessa",
            f"features={features_dir}",
            f"report={report_file}",
        ]
        if filter_:
            params.append(f"filter={filter_}")
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
        args.extend([
            "/Execute", str(self.vanessa_epf),
            "/C", c_params,
        ])

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
                return VanessaResult(
                    errors=[f"Timeout {self.timeout_seconds}s"],
                    duration_ms=(time.perf_counter() - t0) * 1000,
                )
        except FileNotFoundError:
            return VanessaResult(errors=["1cv8c.exe не найден"])

        duration_ms = (time.perf_counter() - t0) * 1000

        result = self._parse_report(report_file)
        result.duration_ms = duration_ms
        result.report_path = str(report_file)
        return result

    @staticmethod
    def _parse_report(report_path: Path) -> VanessaResult:
        result = VanessaResult(report_path=str(report_path))
        if not report_path.exists():
            result.errors.append(f"Отчёт не найден: {report_path}")
            return result

        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as e:
            result.errors.append(f"Парсинг отчёта: {e}")
            return result

        # Vanessa JSON формат (примерный)
        result.scenarios_total = int(data.get("scenarios_total", 0) or 0)
        result.scenarios_passed = int(data.get("scenarios_passed", 0) or 0)
        result.scenarios_failed = int(data.get("scenarios_failed", 0) or 0)
        result.steps_total = int(data.get("steps_total", 0) or 0)
        result.steps_passed = int(data.get("steps_passed", 0) or 0)
        result.steps_failed = int(data.get("steps_failed", 0) or 0)

        for s in (data.get("scenarios") or []):
            result.details.append({
                "name": s.get("name", ""),
                "status": s.get("status", ""),
                "steps_failed": s.get("steps_failed", 0),
                "error": s.get("error", ""),
            })

        result.success = (
            result.scenarios_failed == 0
            and result.steps_failed == 0
            and not result.errors
        )
        return result
