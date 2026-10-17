# ═══════════════════════════════════════════════════════════════════════
# LLM Agent — сборка standalone-лаунчера (PyInstaller, one-file)
#
# ВАЖНО: PyInstaller НЕ умеет кросс-компиляцию.
#   - Windows exe собирается НА Windows:  build_exe.bat
#   - Linux бинарник собирается НА Linux: ./build_exe.sh
#
# Готовый лаунчер (llm-agent.exe / llm-agent) запускает FastAPI-сервер
# как run.py. Декларации/конфиги/веб-интерфейс кладутся рядом (dist/).
# ═══════════════════════════════════════════════════════════════════════
# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=[],
    datas=[
        ("config", "config"),           # settings.yaml, memory.yaml...
        ("agents", "agents"),           # декларации агентов
        ("mcp_servers", "mcp_servers"), # декларации MCP
        ("capabilities", "capabilities"),
        ("loops", "loops"),
        ("src/web", "src/web"),         # index.html, app.js, style.css
        (".env.example", "."),
        ("README.md", "."),
    ],
    hiddenimports=[
        "first_run",
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "src.main",
        "src.config",
        "src.orchestrator",
        "src.mcp_manager",
        "src.policies",
        "src.llm_client",
        "src.cache",
        "src.file_state",
        "src.runtime_config",
        "src.journal.recorder",
        "src.journal.rollback",
        "src.journal.replay",
        "src.journal.retention",
        "src.journal.storage",
        "src.journal.action_graph",
        "src.journal.models",
        "src.core.registry",
        "src.core.loader",
        "src.core.schema",
        "src.core.health",
        "src.core.snapshot",
        "src.core.metrics",
        "src.loop.controller",
        "src.loop.spec",
        "src.loop.telemetry",
        "src.memory.facade",
        "src.agents.runtime",
    ],
    excludes=["tkinter", "matplotlib", "IPython", "jupyter"],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name="llm-agent",
    console=True,
    upx=False,
)
