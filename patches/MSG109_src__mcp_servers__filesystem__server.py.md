# Патч: src/mcp_servers/filesystem/server.py

Исходный заголовок: `## 📄 src/mcp_servers/filesystem/server.py — **ЗАМЕНИТЬ** функции архива`
Сообщение: MSG 109, строка 80939

```python
        Tool(name="extract_archive",
             description="Распаковать архив (безопасно).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "dest": {"type": "string"}},
                 "required": ["path", "dest"]}),
    ]
```

```python
        Tool(name="extract_archive",
             description="Распаковать архив (безопасно).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "dest": {"type": "string"}},
                 "required": ["path", "dest"]}),
        Tool(name="create_7z",
             description="Создать 7z-архив (требует 7z CLI).",
             inputSchema={"type": "object", "properties": {
                 "dest": {"type": "string"},
                 "sources": {"type": "array", "items": {"type": "string"}},
                 "password": {"type": "string"}},
                 "required": ["dest", "sources"]}),
        Tool(name="extract_7z",
             description="Распаковать 7z (требует 7z CLI).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "dest": {"type": "string"},
                 "password": {"type": "string"}},
                 "required": ["path", "dest"]}),
        Tool(name="extract_rar",
             description="Распаковать rar (требует unrar/7z).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "dest": {"type": "string"},
                 "password": {"type": "string"}},
                 "required": ["path", "dest"]}),
    ]
```

```python
        # ─── 7z / rar ────────────────────────────────
        if name == "create_7z":
            dest = _safe(arguments["dest"])
            sources = [_safe(s) for s in arguments["sources"]]
            seven_zip = shutil.which("7z") or shutil.which("7za") or shutil.which("7z.exe")
            if not seven_zip:
                return [TextContent(
                    type="text",
                    text="7z не установлен. Windows: https://www.7-zip.org/",
                )]
            cmd = [seven_zip, "a", str(dest)] + [str(s) for s in sources]
            if arguments.get("password"):
                cmd.append(f"-p{arguments['password']}")
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=300,
                )
                return [TextContent(
                    type="text",
                    text=f"exit={result.returncode}\n{result.stdout[-3000:]}",
                )]
            except Exception as e:
                return [TextContent(type="text", text=f"Ошибка: {e}")]

        if name == "extract_7z":
            p = _safe(arguments["path"])
            dest = _safe(arguments["dest"])
            dest.mkdir(parents=True, exist_ok=True)
            seven_zip = shutil.which("7z") or shutil.which("7za") or shutil.which("7z.exe")
            if not seven_zip:
                return [TextContent(type="text", text="7z не установлен")]
            cmd = [seven_zip, "x", str(p), f"-o{dest}", "-y"]
            if arguments.get("password"):
                cmd.append(f"-p{arguments['password']}")
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=600,
                )
                return [TextContent(
                    type="text",
                    text=f"exit={result.returncode}\n{result.stdout[-3000:]}",
                )]
            except Exception as e:
                return [TextContent(type="text", text=f"Ошибка: {e}")]

        if name == "extract_rar":
            p = _safe(arguments["path"])
            dest = _safe(arguments["dest"])
            dest.mkdir(parents=True, exist_ok=True)
            unrar = shutil.which("unrar") or shutil.which("unrar.exe")
            seven_zip = shutil.which("7z") or shutil.which("7za") or shutil.which("7z.exe")
            if unrar:
                cmd = [unrar, "x", str(p), str(dest), "-y"]
                if arguments.get("password"):
                    cmd.append(f"-p{arguments['password']}")
            elif seven_zip:
                cmd = [seven_zip, "x", str(p), f"-o{dest}", "-y"]
                if arguments.get("password"):
                    cmd.append(f"-p{arguments['password']}")
            else:
                return [TextContent(
                    type="text", text="unrar и 7z не найдены",
                )]
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=600,
                )
                return [TextContent(
                    type="text",
                    text=f"exit={result.returncode}\n{result.stdout[-3000:]}",
                )]
            except Exception as e:
                return [TextContent(type="text", text=f"Ошибка: {e}")]

```

```python
import subprocess
```
