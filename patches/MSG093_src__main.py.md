# Патч: src/main.py

Исходный заголовок: `## 📄 src/main.py (дополнения для cluster)`
Сообщение: MSG 93, строка 59176

```python
# после state.update(...)
from src.cluster.config import ClusterConfig
from src.cluster.redis_bus import RedisBus
from src.cluster.heartbeat import heartbeat_loop
from src.cluster.ws_bridge import WsBridge

cluster_cfg = ClusterConfig()
redis_bus = None
ws_bridge = None
heartbeat_task = None

if cluster_cfg.enabled:
    redis_bus = RedisBus(cluster_cfg)
    connected = await redis_bus.connect()
    if connected:
        ws_bridge = WsBridge(redis_bus, cluster_cfg)

        # Подписка на snapshot events
        async def _on_snapshot(payload):
            reg: Registry = state.get("registry")
            if reg:
                await reg.build_snapshot(reason="remote_event")
                await _sync_runtime()

        await redis_bus.subscribe(
            cluster_cfg.channel_snapshot, _on_snapshot,
        )

        heartbeat_task = asyncio.create_task(
            heartbeat_loop(
                redis_bus, cluster_cfg,
                lambda: {
                    "agents": len(state.get("agent_runtime").agents)
                        if state.get("agent_runtime") else 0,
                },
            )
        )

state["cluster_cfg"] = cluster_cfg
state["redis_bus"] = redis_bus
state["ws_bridge"] = ws_bridge

# В finally:
if heartbeat_task:
    heartbeat_task.cancel()
if redis_bus:
    await redis_bus.close()
```

```python
# в начале endpoint
ws_bridge: WsBridge = state.get("ws_bridge")
session_id = f"ws_{uuid.uuid4().hex[:12]}"

if ws_bridge:
    await ws_bridge.register_session(session_id, websocket)

# ... в finally
if ws_bridge:
    ws_bridge.unregister_session(session_id)
```
