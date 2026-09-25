"""FeatureLoader — «всё — директория с манифестом» (Этап 4, V2 §2.2).

Новая подсистема (feature) = директория features/<id>/ с манифестом
feature.yaml:

    id: journal
    version: "1.0.0"
    title: Журнал действий
    enabled: true
    requires:
      python_packages: [{name: psycopg, level: soft}]
    api_router: src.journal.api:build_router   # module:func ИЛИ file:func
    ui_tab: {file: ui.js, title: Журнал, icon: "📓"}
    ws_events: [journal.event]
    migrations: migrations/                    # прогон при первом старте

FeatureLoader при старте:
1. сканирует features/*/feature.yaml (pydantic-валидация; ошибка одной
   декларации не ломает остальные — как в DeclarationLoader);
2. проверяет requires (hard-пакет отсутствует → фича пропускается,
   soft → предупреждение);
3. импортирует api_router и делает app.include_router();
4. кладёт вкладку UI в реестр: GET /api/features →
   [{id, title, icon, js_url, ...}]; app.js подхватывает js_url сам;
5. при наличии migrations/ прогоняет *.py один раз (маркер в
   data/features/<id>.migrations.json).

Итог: ноль правок main.py / index.html для новой подсистемы.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

FEATURES_DIR = "features"


class PackageReq(BaseModel):
    """Требование к python-пакету: level hard|soft."""

    name: str
    level: str = "hard"


class RequiresSpec(BaseModel):
    python_packages: list[PackageReq] = Field(default_factory=list)


class UiTabSpec(BaseModel):
    """Самодостаточная вкладка UI: js-файл монтируется сам."""

    file: str
    title: str = ""
    icon: str = "🧩"


class FeatureManifest(BaseModel):
    """Манифест подсистемы features/<id>/feature.yaml."""

    id: str
    version: str = "0.0.0"
    title: str = ""
    description: str = ""
    enabled: bool = True
    requires: RequiresSpec = Field(default_factory=RequiresSpec)
    api_router: str | None = None       # "module:func" | "file.py:func"
    ui_tab: UiTabSpec | None = None
    ws_events: list[str] = Field(default_factory=list)
    mcp_server: str | None = None       # путь к server.yaml (декларативно)
    migrations: str | None = None       # каталог с *.py, прогон один раз
    permissions: dict[str, Any] = Field(default_factory=dict)


class FeatureLoader:
    """Сканирует features/, валидирует манифесты, монтирует в FastAPI."""

    def __init__(self, base_dir: str | Path,
                 data_dir: str | Path | None = None):
        self.base_dir = Path(base_dir)
        self.features_dir = self.base_dir / FEATURES_DIR
        self.data_dir = Path(data_dir) if data_dir else (
            self.base_dir / "data" / "features")
        #: реестр для GET /api/features: id → карточка
        self.registry: dict[str, dict] = {}
        #: отчёт о загрузке (диагностика / лог старта)
        self.load_report: list[dict] = []

    # ═══════════════════════════════════════════════════════
    # Сканирование
    # ═══════════════════════════════════════════════════════
    def scan(self) -> list[tuple[FeatureManifest, Path]]:
        """Валидные (манифест, путь); ошибки изолированы."""
        out: list[tuple[FeatureManifest, Path]] = []
        if not self.features_dir.is_dir():
            return out
        for child in sorted(self.features_dir.iterdir()):
            if not child.is_dir():
                continue
            mf = child / "feature.yaml"
            if not mf.exists():
                continue
            try:
                data = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
                manifest = FeatureManifest.model_validate(data)
            except ValidationError as e:
                logger.warning("Feature '%s': невалидный манифест: %s",
                               child.name, e)
                self.load_report.append({
                    "id": child.name, "ok": False,
                    "error": f"манифест: {e}", })
                continue
            except Exception as e:
                logger.warning("Feature '%s': не читается: %s", child.name, e)
                self.load_report.append({
                    "id": child.name, "ok": False, "error": str(e)})
                continue
            out.append((manifest, child))
        return out

    # ═══════════════════════════════════════════════════════
    # Монтаж
    # ═══════════════════════════════════════════════════════
    async def mount(self, app: FastAPI, state: dict) -> list[dict]:
        """Подключает включённые фичи к приложению (вызывается в lifespan)."""
        for manifest, path in self.scan():
            entry: dict = {
                "id": manifest.id,
                "title": manifest.title or manifest.id,
                "icon": manifest.ui_tab.icon if manifest.ui_tab else "🧩",
                "version": manifest.version,
                "enabled": manifest.enabled,
                "ws_events": list(manifest.ws_events),
                "js_url": "",
                "api_mounted": False,
            }
            if not manifest.enabled:
                entry["error"] = "отключена манифестом"
                self.load_report.append({"id": manifest.id, "ok": True,
                                         "skipped": "disabled"})
                self.registry[manifest.id] = entry
                continue

            err = self._check_requires(manifest)
            if err:
                entry["error"] = err
                self.load_report.append({"id": manifest.id, "ok": False,
                                         "error": err})
                self.registry[manifest.id] = entry
                continue

            # API-роутер
            if manifest.api_router:
                try:
                    router = self._import_router(manifest.api_router, path)
                    app.include_router(router)
                    entry["api_mounted"] = True
                except Exception as e:
                    logger.warning("Feature '%s': api_router не смонтирован: %s",
                                   manifest.id, e)
                    entry.setdefault("warnings", []).append(
                        f"api_router: {e}")

            # Вкладка UI
            if manifest.ui_tab:
                ui_file = path / manifest.ui_tab.file
                if ui_file.exists():
                    entry["js_url"] = f"/{FEATURES_DIR}/{manifest.id}/" \
                                      f"{manifest.ui_tab.file}"
                else:
                    entry.setdefault("warnings", []).append(
                        f"ui-файл не найден: {manifest.ui_tab.file}")

            # Миграции (один раз)
            if manifest.migrations:
                try:
                    await self._run_migrations(manifest, path, state)
                except Exception as e:
                    entry.setdefault("warnings", []).append(f"migrations: {e}")

            self.load_report.append({"id": manifest.id, "ok": True})
            self.registry[manifest.id] = entry
            logger.info(
                "Feature '%s' подключена (api=%s, ui=%s)",
                manifest.id, bool(entry["api_mounted"]), bool(entry["js_url"]),
            )
        return self.load_report

    def _check_requires(self, manifest: FeatureManifest) -> str:
        """'' если требования удовлетворены; иначе причина пропуска."""
        for req in manifest.requires.python_packages:
            spec = importlib.util.find_spec(req.name)
            if spec is not None:
                continue
            if req.level == "hard":
                return (f"пакет '{req.name}' не установлен "
                        f"(hard-требование)")
            logger.info("Feature '%s': пакет '%s' не установлен (soft) — "
                        "часть функций может не работать",
                        manifest.id, req.name)
        return ""

    def _import_router(self, spec: str, feature_path: Path):
        """api_router: 'module:func' (импорт из пакета) или 'file.py:func'
        (файл рядом с манифестом). func может быть фабрикой роутера
        (create_router()) или уже готовым APIRouter'ом."""
        target, _, attr = spec.partition(":")
        attr = attr or "create_router"
        if target.endswith(".py"):
            file_path = (feature_path / target).resolve()
            mod_name = f"llm_feature_{feature_path.name}_{file_path.stem}"
            fspec = importlib.util.spec_from_file_location(mod_name, file_path)
            if fspec is None or fspec.loader is None:
                raise ImportError(f"не могу загрузить {file_path}")
            module = importlib.util.module_from_spec(fspec)
            sys.modules[mod_name] = module
            fspec.loader.exec_module(module)
        else:
            module = importlib.import_module(target)
        obj = getattr(module, attr)
        if hasattr(obj, "routes"):          # уже APIRouter
            return obj
        if callable(obj):                    # фабрика роутера
            obj = obj()
            if hasattr(obj, "routes"):
                return obj
        raise TypeError(f"api_router '{spec}': это не APIRouter")

    async def _run_migrations(self, manifest: FeatureManifest,
                              feature_path: Path, state: dict) -> None:
        """Прогоняет migrations/*.py один раз (маркер в data/features)."""
        mig_dir = feature_path / manifest.migrations
        if not mig_dir.is_dir():
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        marker = self.data_dir / f"{manifest.id}.migrations.json"
        applied: set[str] = set()
        if marker.exists():
            try:
                applied = set(json.loads(
                    marker.read_text(encoding="utf-8")))
            except Exception:
                applied = set()
        for mig in sorted(mig_dir.glob("*.py")):
            if mig.name in applied:
                continue
            fspec = importlib.util.spec_from_file_location(
                f"llm_mig_{manifest.id}_{mig.stem}", mig)
            module = importlib.util.module_from_spec(fspec)
            try:
                fspec.loader.exec_module(module)
                runner = getattr(module, "run", None)
                if runner is not None:
                    result = runner(state)
                    if asyncio.iscoroutine(result):
                        await result
                applied.add(mig.name)
                marker.write_text(
                    json.dumps(sorted(applied)), encoding="utf-8")
                logger.info("Feature '%s': миграция %s применена",
                            manifest.id, mig.name)
            except Exception:
                logger.warning("Feature '%s': миграция %s упала (пропускаю)",
                               manifest.id, mig.name, exc_info=True)
                break   # порядок важен: остальные не применяем

    # ═══════════════════════════════════════════════════════
    # Список для клиента
    # ═══════════════════════════════════════════════════════
    def list_for_client(self) -> list[dict]:
        """GET /api/features: карточки вкладок фич (только рабочие)."""
        out = []
        for entry in self.registry.values():
            if not entry.get("enabled") or entry.get("error"):
                continue
            item = {k: entry.get(k, "") for k in
                    ("id", "title", "icon", "version", "js_url")}
            item["ws_events"] = entry.get("ws_events", [])
            out.append(item)
        return out
