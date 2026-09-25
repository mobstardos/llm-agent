"""Определение тестового фреймворка по файлам проекта."""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def detect_framework(root: Path) -> str:
    """Возвращает: pytest | unittest | jest | vitest | go | cargo | ..."""
    if (root / "package.json").exists():
        try:
            pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "vitest" in deps:
                return "vitest"
            if "jest" in deps:
                return "jest"
            if "mocha" in deps:
                return "mocha"
        except Exception:
            pass

    if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists():
        return "pytest"
    if (root / "setup.py").exists() or (root / "setup.cfg").exists():
        return "pytest"

    for p in root.glob("test_*.py"):
        return "pytest"
    for p in root.glob("*_test.py"):
        return "pytest"
    for p in (root / "tests").rglob("test_*.py") if (root / "tests").exists() else []:
        return "pytest"

    if (root / "Cargo.toml").exists():
        return "cargo"
    if (root / "go.mod").exists():
        return "go"
    if (root / "pom.xml").exists() or (root / "build.gradle").exists():
        return "maven"

    return ""


def framework_commands(fw: str) -> dict:
    """Команды для фреймворка."""
    cmds = {
        "pytest": {
            "run": ["python", "-m", "pytest", "-v"],
            "single": ["python", "-m", "pytest", "-v"],
            "discover": ["python", "-m", "pytest", "--collect-only", "-q"],
            "coverage": ["python", "-m", "pytest", "--cov", "--cov-report=json"],
        },
        "unittest": {
            "run": ["python", "-m", "unittest", "discover", "-v"],
            "single": ["python", "-m", "unittest", "-v"],
            "discover": ["python", "-m", "unittest", "discover", "-v"],
            "coverage": ["python", "-m", "coverage", "run", "-m", "unittest", "discover"],
        },
        "jest": {
            "run": ["npx", "jest", "--json"],
            "single": ["npx", "jest"],
            "discover": ["npx", "jest", "--listTests", "--json"],
            "coverage": ["npx", "jest", "--coverage", "--json"],
        },
        "vitest": {
            "run": ["npx", "vitest", "run", "--reporter=json"],
            "single": ["npx", "vitest", "run"],
            "discover": ["npx", "vitest", "list"],
            "coverage": ["npx", "vitest", "run", "--coverage"],
        },
        "cargo": {
            "run": ["cargo", "test", "--", "--nocapture"],
            "single": ["cargo", "test"],
            "discover": ["cargo", "test", "--", "--list"],
            "coverage": ["cargo", "test"],
        },
        "go": {
            "run": ["go", "test", "-v", "./..."],
            "single": ["go", "test", "-v"],
            "discover": ["go", "test", "-list", ".*", "./..."],
            "coverage": ["go", "test", "-cover", "./..."],
        },
    }
    return cmds.get(fw, {})
