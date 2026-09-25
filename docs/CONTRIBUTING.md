# Как добавить нового агента

1. **Создать MCP-сервер** в `src/mcp_servers/`.
   Он описывает набор инструментов (tools) через декораторы `@app.list_tools()`
   и `@app.call_tool()`.

2. **Зарегистрировать MCP-сервер** в `config/settings.yaml` → `mcp_servers`:
   