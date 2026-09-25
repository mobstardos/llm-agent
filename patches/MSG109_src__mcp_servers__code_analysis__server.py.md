# Патч: src/mcp_servers/code_analysis/server.py

Исходный заголовок: `## 📄 src/mcp_servers/code_analysis/server.py — **ЗАМЕНИТЬ** константу`
Сообщение: MSG 109, строка 81730

```python
CODE_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
```

```python
CODE_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".cs", ".php", ".rb",
}
```
