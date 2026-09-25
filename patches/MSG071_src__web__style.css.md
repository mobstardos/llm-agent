# Патч: src/web/style.css

Исходный заголовок: `## 📄 src/web/style.css — дополнения`
Сообщение: MSG 71, строка 33718

```css
/* ─── Settings modal ────────────────────────────── */
.settings-content { max-width: 900px; max-height: 90vh; display: flex; flex-direction: column; }
.settings-header { border-bottom: 1px solid #23252e; padding-bottom: 12px; margin-bottom: 16px; }
.settings-header h3 { margin: 0 0 12px 0; }
.settings-tabs { display: flex; gap: 4px; flex-wrap: wrap; }
.tab-btn {
  background: transparent; color: #9aa0a6; border: 1px solid transparent;
  padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 13px;
}
.tab-btn:hover { background: #23252e; color: #e6e6e6; }
.tab-btn.active { background: #1c1f28; color: #a5d6ff; border-color: #3b82f6; }
.settings-body { flex: 1; overflow-y: auto; padding-right: 4px; }
.tab-panel { display: none; }
.tab-panel.active { display: block; }

.panel-actions { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.summary { color: #6b7280; font-size: 12px; }

.list { display: flex; flex-direction: column; gap: 10px; }

/* ─── Agent/MCP cards ───────────────────────────── */
.agent-card, .mcp-card {
  background: #0f1116; border: 1px solid #23252e; border-radius: 8px;
  padding: 12px 14px;
}
.agent-card.status-active { border-left: 3px solid #4ade80; }
.agent-card.status-degraded { border-left: 3px solid #fbbf24; }
.agent-card.status-unavailable, .agent-card.status-failed { border-left: 3px solid #f87171; }
.agent-card.status-disabled { border-left: 3px solid #4b5563; opacity: 0.75; }
.mcp-card.disabled { opacity: 0.6; }

.agent-header { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.agent-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.agent-icon { font-size: 18px; }
.agent-name { font-weight: 600; }
.agent-id { background: #171a22; padding: 2px 6px; border-radius: 4px; font-size: 11px; color: #a5d6ff; }
.agent-desc { color: #9aa0a6; font-size: 12px; margin-top: 6px; line-height: 1.4; }
.agent-actions { display: flex; gap: 6px; margin-top: 10px; flex-wrap: wrap; }
.agent-actions button { padding: 4px 10px; font-size: 11px; }

.reasons { margin-top: 8px; }
.reason { color: #fca5a5; font-size: 11px; margin-bottom: 2px; }

.badge {
  font-size: 10px; padding: 2px 8px; border-radius: 999px;
  font-weight: 600; text-transform: uppercase; letter-spacing: 0.3px;
}
.badge-green { background: #14532d; color: #86efac; }
.badge-yellow { background: #78350f; color: #fcd34d; }
.badge-red { background: #7f1d1d; color: #fca5a5; }
.badge-gray { background: #374151; color: #9ca3af; }
.badge-blue { background: #1e3a8a; color: #93c5fd; }

/* Switch */
.switch { position: relative; display: inline-block; width: 40px; height: 22px; }
.switch input { opacity: 0; width: 0; height: 0; }
.slider {
  position: absolute; cursor: pointer; inset: 0; background: #374151;
  transition: 0.2s; border-radius: 22px;
}
.slider:before {
  content: ""; position: absolute; height: 16px; width: 16px;
  left: 3px; bottom: 3px; background: white; transition: 0.2s; border-radius: 50%;
}
.switch input:checked + .slider { background: #16a34a; }
.switch input:checked + .slider:before { transform: translateX(18px); }
.switch input:disabled + .slider { opacity: 0.5; cursor: not-allowed; }

/* Params form */
.params-form {
  background: #171a22; border-radius: 6px; padding: 12px;
  margin-top: 10px;
}
.form-row { display: flex; flex-direction: column; gap: 4px; margin-bottom: 10px; }
.form-row label { font-size: 11px; color: #9aa0a6; text-transform: uppercase; letter-spacing: 0.3px; }
.form-row input, .form-row textarea {
  background: #0f1116; color: #e6e6e6; border: 1px solid #23252e;
  padding: 6px 10px; border-radius: 6px; font-size: 13px;
}
.form-row textarea { resize: vertical; font-family: inherit; }
.params-actions { display: flex; justify-content: flex-end; margin-top: 8px; }
.params-actions button { padding: 6px 14px; font-size: 12px; }

/* MCP tool chips */
.mcp-cmd { margin-top: 8px; font-size: 11px; color: #6b7280; }
.mcp-tools { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 8px; }
.tool-chip {
  background: #171a22; border: 1px solid #23252e; padding: 2px 6px;
  border-radius: 4px; font-size: 10px; color: #a5d6ff;
  font-family: ui-monospace, monospace;
}

/* Capabilities */
.cap-resolved {
  background: #171a22; border-radius: 6px; padding: 10px;
  margin-top: 8px; font-size: 12px;
}
.cap-providers { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
.provider-chip {
  background: #171a22; border: 1px solid #23252e; padding: 6px 10px;
  border-radius: 6px; font-size: 11px; display: flex; flex-direction: column;
  gap: 2px;
}
.provider-chip small { color: #6b7280; font-size: 10px; }

.stats {
  background: #0f1116; border-radius: 6px; padding: 12px;
  font-family: ui-monospace, monospace; font-size: 11px;
  white-space: pre-wrap; color: #a5d6ff;
  max-height: 300px; overflow-y: auto;
}
.form-group { margin-bottom: 16px; }
.form-group label { display: block; font-size: 12px; color: #9aa0a6; margin-bottom: 6px; }
.input {
  width: 100%; background: #0f1116; color: #e6e6e6; border: 1px solid #23252e;
  padding: 10px 12px; border-radius: 6px; font-family: ui-monospace, monospace;
  font-size: 13px; box-sizing: border-box;
}
.actions { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
```

```
                     ┌──────────────────────────────────┐
                     │           Registry               │
                     └────────────┬─────────────────────┘
                                  │
        ┌────────────┬────────────┼────────────┬────────────┐
        ▼            ▼            ▼            ▼            ▼
  MigrationEngine  History    ProfileStore  AuditStore  RollbackStore
        │            │            │            │            │
        │            │            │            │            │
        └──────┬─────┴──────┬─────┴──────┬─────┴──────┬─────┘
               │            │            │            │
               ▼            ▼            ▼            ▼
          snapshots/    profiles/     audit.db    backups/
          a.json        dev.json                  agents/
          b.json        1c.json                   mcp_servers/
                        prod.json                 capabilities/

                            ▲
                            │
                     ┌──────┴──────┐
                     │             │
                CLI-валидатор  UI Diagnostics
                     │             │
                     └──────┬──────┘
                            ▼
                     Prometheus /metrics
```
