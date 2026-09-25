"""MCP-сервер: обработка данных (CSV, Parquet, JSON, YAML, TOML, XML)."""
from __future__ import annotations

import json
import asyncio
import logging
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("data-mcp")


def _get_root() -> Path:
    try:
        from src.runtime_config import get_project_root
        val = get_project_root(default="")
        if val:
            return Path(val).resolve()
    except Exception:
        pass
    return Path(os.getenv("PROJECT_ROOT", os.getcwd())).resolve()


def _safe(path: str) -> Path:
    root = _get_root()
    p = (root / path).resolve()
    if root not in p.parents and p != root:
        raise ValueError(f"Путь вне корня: {path}")
    return p


def _json_result(data) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)[:50000]
    except Exception:
        return str(data)[:50000]


app = Server("data")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="data_info",
             description="Доступные библиотеки обработки данных.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="csv_read",
             description="Прочитать CSV (первые N строк).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "limit": {"type": "integer", "default": 50}},
                 "required": ["path"]}),
        Tool(name="csv_query",
             description="SQL-запрос к CSV через DuckDB.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "sql": {"type": "string"},
                 "limit": {"type": "integer", "default": 100}},
                 "required": ["path", "sql"]}),
        Tool(name="parquet_read",
             description="Прочитать Parquet.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "limit": {"type": "integer", "default": 50}},
                 "required": ["path"]}),
        Tool(name="parquet_query",
             description="SQL-запрос к Parquet через DuckDB.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "sql": {"type": "string"},
                 "limit": {"type": "integer", "default": 100}},
                 "required": ["path", "sql"]}),
        Tool(name="json_query",
             description="JSONPath-подобный запрос к JSON.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "jsonpath": {"type": "string", "default": ""}},
                 "required": ["path"]}),
        Tool(name="json_schema",
             description="Схема JSON (типы, ключи).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"}},
                 "required": ["path"]}),
        Tool(name="data_diff",
             description="Diff двух JSON/CSV/YAML файлов.",
             inputSchema={"type": "object", "properties": {
                 "path_a": {"type": "string"},
                 "path_b": {"type": "string"}},
                 "required": ["path_a", "path_b"]}),
        Tool(name="csv_to_json",
             description="Конвертировать CSV → JSON.",
             inputSchema={"type": "object", "properties": {
                 "src": {"type": "string"},
                 "dst": {"type": "string"}},
                 "required": ["src", "dst"]}),
        Tool(name="csv_to_parquet",
             description="Конвертировать CSV → Parquet.",
             inputSchema={"type": "object", "properties": {
                 "src": {"type": "string"},
                 "dst": {"type": "string"}},
                 "required": ["src", "dst"]}),
        Tool(name="yaml_validate",
             description="Проверить YAML.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"}},
                 "required": ["path"]}),
        Tool(name="toml_validate",
             description="Проверить TOML.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"}},
                 "required": ["path"]}),
        Tool(name="xml_query",
             description="XPath-запрос к XML.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "xpath": {"type": "string"}},
                 "required": ["path", "xpath"]}),
    ]


def _jsonpath_get(data, path: str):
    """Простой JSONPath: a.b.c[0].d"""
    if not path:
        return data
    parts = path.replace("[", ".").replace("]", "").split(".")
    cur = data
    for p in parts:
        if not p:
            continue
        if isinstance(cur, dict):
            cur = cur.get(p)
        elif isinstance(cur, list):
            try:
                cur = cur[int(p)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def _json_schema_of(data, max_depth: int = 5, depth: int = 0):
    """Рекурсивно выводит типы."""
    if depth >= max_depth:
        return "..."

    if isinstance(data, dict):
        return {
            k: _json_schema_of(v, max_depth, depth + 1)
            for k, v in list(data.items())[:30]
        }
    if isinstance(data, list):
        if not data:
            return ["<empty>"]
        return [_json_schema_of(data[0], max_depth, depth + 1)]
    if data is None:
        return "null"
    return type(data).__name__


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "data_info":
            available = {}
            for lib in ("duckdb", "pandas", "pyarrow", "yaml", "tomllib",
                        "lxml"):
                try:
                    __import__(lib)
                    available[lib] = True
                except ImportError:
                    available[lib] = False
            return [TextContent(
                type="text", text=_json_result(available),
            )]

        # ═══════════════════════════════════════════════════════
        # CSV
        # ═══════════════════════════════════════════════════════
        if name == "csv_read":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            limit = arguments.get("limit", 50)

            try:
                import pandas as pd
                df = pd.read_csv(p, nrows=limit)
                return [TextContent(
                    type="text",
                    text=_json_result({
                        "columns": list(df.columns),
                        "rows_count_preview": len(df),
                        "dtypes": {c: str(t) for c, t in df.dtypes.items()},
                        "preview": df.head(20).to_dict(orient="records"),
                    }),
                )]
            except ImportError:
                import csv
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    reader = csv.reader(f)
                    rows = []
                    for i, row in enumerate(reader):
                        if i >= limit:
                            break
                        rows.append(row)
                return [TextContent(
                    type="text", text=_json_result(rows),
                )]

        if name == "csv_query":
            try:
                import duckdb
            except ImportError:
                return [TextContent(
                    type="text",
                    text="duckdb не установлен: pip install duckdb",
                )]
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]

            sql = arguments["sql"]
            limit = arguments.get("limit", 100)

            try:
                conn = duckdb.connect()
                path_str = str(p).replace("\\", "/")
                # Если SQL не содержит FROM — добавляем автоматически
                if "from" not in sql.lower():
                    sql = f"SELECT {sql} FROM read_csv_auto('{path_str}')"
                elif "{" not in sql and "read_csv" not in sql.lower():
                    sql = sql.replace("FROM ", f"FROM read_csv_auto('{path_str}') ")

                result = conn.execute(sql).fetchall()
                columns = [d[0] for d in conn.description]
                rows = [dict(zip(columns, r)) for r in result[:limit]]
                return [TextContent(
                    type="text",
                    text=_json_result({
                        "columns": columns,
                        "row_count": len(rows),
                        "rows": rows,
                    }),
                )]
            except Exception as e:
                return [TextContent(type="text", text=f"SQL error: {e}")]

        # ═══════════════════════════════════════════════════════
        # Parquet
        # ═══════════════════════════════════════════════════════
        if name in ("parquet_read", "parquet_query"):
            try:
                import duckdb
            except ImportError:
                return [TextContent(type="text", text="duckdb не установлен")]

            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            path_str = str(p).replace("\\", "/")

            try:
                conn = duckdb.connect()
                if name == "parquet_read":
                    limit = arguments.get("limit", 50)
                    result = conn.execute(
                        f"SELECT * FROM read_parquet('{path_str}') LIMIT {limit}"
                    ).fetchall()
                    columns = [d[0] for d in conn.description]
                else:
                    sql = arguments["sql"]
                    if "from" not in sql.lower():
                        sql = f"SELECT {sql} FROM read_parquet('{path_str}')"
                    elif "read_parquet" not in sql.lower():
                        sql = sql.replace(
                            "FROM ", f"FROM read_parquet('{path_str}') ",
                        )
                    result = conn.execute(sql).fetchall()[:arguments.get("limit", 100)]
                    columns = [d[0] for d in conn.description]

                rows = [dict(zip(columns, r)) for r in result]
                return [TextContent(
                    type="text",
                    text=_json_result({
                        "columns": columns,
                        "row_count": len(rows),
                        "rows": rows,
                    }),
                )]
            except Exception as e:
                return [TextContent(type="text", text=f"Error: {e}")]

        # ═══════════════════════════════════════════════════════
        # JSON
        # ═══════════════════════════════════════════════════════
        if name == "json_query":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                return [TextContent(type="text", text=f"Parse: {e}")]

            path = arguments.get("jsonpath", "")
            result = _jsonpath_get(data, path)
            return [TextContent(
                type="text", text=_json_result(result),
            )]

        if name == "json_schema":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                return [TextContent(type="text", text=f"Parse: {e}")]
            return [TextContent(
                type="text", text=_json_result(_json_schema_of(data)),
            )]

        # ═══════════════════════════════════════════════════════
        # Diff
        # ═══════════════════════════════════════════════════════
        if name == "data_diff":
            pa = _safe(arguments["path_a"])
            pb = _safe(arguments["path_b"])
            if not pa.exists() or not pb.exists():
                return [TextContent(type="text", text="Файлы не найдены")]

            def load_any(p: Path):
                text = p.read_text(encoding="utf-8", errors="replace")
                if p.suffix == ".json":
                    try:
                        return json.loads(text)
                    except Exception:
                        pass
                if p.suffix in (".yaml", ".yml"):
                    try:
                        import yaml
                        return yaml.safe_load(text)
                    except Exception:
                        pass
                if p.suffix == ".csv":
                    try:
                        import csv
                        return list(csv.DictReader(text.splitlines()))
                    except Exception:
                        pass
                return text

            a = load_any(pa)
            b = load_any(pb)

            # Простой diff по ключам для dict
            def diff(a, b, path=""):
                changes = []
                if isinstance(a, dict) and isinstance(b, dict):
                    all_keys = set(a) | set(b)
                    for k in sorted(all_keys):
                        if k not in a:
                            changes.append({"op": "add", "path": f"{path}.{k}", "value": b[k]})
                        elif k not in b:
                            changes.append({"op": "remove", "path": f"{path}.{k}", "value": a[k]})
                        elif a[k] != b[k]:
                            changes.extend(diff(a[k], b[k], f"{path}.{k}"))
                elif a != b:
                    changes.append({
                        "op": "change", "path": path,
                        "before": a, "after": b,
                    })
                return changes

            changes = diff(a, b)
            return [TextContent(
                type="text",
                text=_json_result({
                    "changes_count": len(changes),
                    "changes": changes[:100],
                }),
            )]

        # ═══════════════════════════════════════════════════════
        # Convert
        # ═══════════════════════════════════════════════════════
        if name == "csv_to_json":
            src = _safe(arguments["src"])
            dst = _safe(arguments["dst"])
            if not src.exists():
                return [TextContent(type="text", text="Файл не найден")]
            try:
                import pandas as pd
                df = pd.read_csv(src)
                data = df.to_dict(orient="records")
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                return [TextContent(
                    type="text",
                    text=f"Сохранено: {dst} ({len(data)} записей)",
                )]
            except ImportError:
                return [TextContent(
                    type="text", text="pandas не установлен",
                )]

        if name == "csv_to_parquet":
            src = _safe(arguments["src"])
            dst = _safe(arguments["dst"])
            if not src.exists():
                return [TextContent(type="text", text="Файл не найден")]
            try:
                import pandas as pd
                df = pd.read_csv(src)
                dst.parent.mkdir(parents=True, exist_ok=True)
                df.to_parquet(dst)
                return [TextContent(
                    type="text",
                    text=f"Сохранено: {dst} ({len(df)} записей)",
                )]
            except ImportError:
                return [TextContent(
                    type="text", text="pandas+pyarrow не установлены",
                )]

        # ═══════════════════════════════════════════════════════
        # Validate
        # ═══════════════════════════════════════════════════════
        if name == "yaml_validate":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            try:
                import yaml
                data = yaml.safe_load(p.read_text(encoding="utf-8"))
                return [TextContent(
                    type="text",
                    text=_json_result({
                        "valid": True,
                        "top_type": type(data).__name__,
                    }),
                )]
            except Exception as e:
                return [TextContent(
                    type="text",
                    text=_json_result({"valid": False, "error": str(e)}),
                )]

        if name == "toml_validate":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            try:
                try:
                    import tomllib
                    data = tomllib.loads(p.read_text(encoding="utf-8"))
                except ImportError:
                    import toml
                    data = toml.loads(p.read_text(encoding="utf-8"))
                return [TextContent(
                    type="text",
                    text=_json_result({
                        "valid": True,
                        "keys": list(data.keys()) if isinstance(data, dict) else [],
                    }),
                )]
            except Exception as e:
                return [TextContent(
                    type="text",
                    text=_json_result({"valid": False, "error": str(e)}),
                )]

        if name == "xml_query":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            try:
                from lxml import etree
                tree = etree.parse(str(p))
                result = tree.xpath(arguments["xpath"])
                # Сериализуем результаты
                out = []
                for r in result[:100]:
                    if isinstance(r, etree._Element):
                        out.append(etree.tostring(r, encoding="unicode")[:1000])
                    else:
                        out.append(str(r)[:1000])
                return [TextContent(
                    type="text",
                    text=_json_result({"count": len(result), "results": out}),
                )]
            except Exception as e:
                return [TextContent(type="text", text=f"XPath: {e}")]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("data tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
