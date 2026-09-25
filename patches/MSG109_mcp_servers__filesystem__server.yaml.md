# Патч: mcp_servers/filesystem/server.yaml

Исходный заголовок: `## 📄 mcp_servers/filesystem/server.yaml — **ЗАМЕНИТЬ** список tools`
Сообщение: MSG 109, строка 80916

```yaml
  # ─── Архивы ─────────────────────────────────
  - {name: list_archive, danger: read}
  - {name: create_archive, danger: write}
  - {name: extract_archive, danger: destructive}
```

```yaml
  # ─── Архивы ─────────────────────────────────
  - {name: list_archive, danger: read}
  - {name: create_archive, danger: write}
  - {name: extract_archive, danger: destructive}
  - {name: extract_7z, danger: destructive}
  - {name: create_7z, danger: write}
  - {name: extract_rar, danger: destructive}
```
