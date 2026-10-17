"""История snapshot'ов: A/B слоты, diff между сборками, retention."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_SNAPSHOTS = 100


@dataclass
class SnapshotRecord:
    id: str
    built_at: float
    slot: str
    agents: dict[str, str] = field(default_factory=dict)
    mcp: dict[str, bool] = field(default_factory=dict)
    capabilities: dict[str, str] = field(default_factory=dict)
    prompt_length: int = 0
    prompt_hash: str = ""
    reason: str = "manual"


@dataclass
class SnapshotDiff:
    from_id: str
    to_id: str
    agents_added: list[str] = field(default_factory=list)
    agents_removed: list[str] = field(default_factory=list)
    agents_status_changed: list[dict] = field(default_factory=list)
    mcp_changed: list[dict] = field(default_factory=list)
    capabilities_changed: list[dict] = field(default_factory=list)
    prompt_changed: bool = False

    @property
    def has_changes(self) -> bool:
        return bool(
            self.agents_added or self.agents_removed
            or self.agents_status_changed or self.mcp_changed
            or self.capabilities_changed or self.prompt_changed
        )

    def to_dict(self) -> dict:
        return {
            "from": self.from_id,
            "to": self.to_id,
            "agents_added": self.agents_added,
            "agents_removed": self.agents_removed,
            "agents_status_changed": self.agents_status_changed,
            "mcp_changed": self.mcp_changed,
            "capabilities_changed": self.capabilities_changed,
            "prompt_changed": self.prompt_changed,
            "has_changes": self.has_changes,
        }


class SnapshotHistory:
    """Хранит последние N snapshot'ов на диске."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._cache: list[SnapshotRecord] = []
        self._loaded = False

    # ═══════════════════════════════════════════════════════
    # Write
    # ═══════════════════════════════════════════════════════
    def record(self, snapshot, reason: str = "manual") -> SnapshotRecord:
        prompt = getattr(snapshot, "orchestrator_prompt", "") or ""

        rec = SnapshotRecord(
            id=f"snap_{int(time.time() * 1000)}",
            built_at=snapshot.built_at,
            slot=self._next_slot(),
            agents={
                aid: (st.status.value if hasattr(st.status, "value")
                      else str(st.status))
                for aid, st in snapshot.agents.items()
            },
            mcp={
                mid: bool(st.alive) for mid, st in snapshot.mcp_servers.items()
            },
            capabilities={
                cid: (r.get("primary") or "")
                for cid, r in snapshot.resolved_capabilities.items()
            },
            prompt_length=len(prompt),
            prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
            reason=reason,
        )
        self._write(rec)
        self._cache.append(rec)
        self._prune()
        return rec

    def _next_slot(self) -> str:
        if not self._cache:
            self._load()
        if not self._cache:
            return "a"
        return "b" if self._cache[-1].slot == "a" else "a"

    def _write(self, rec: SnapshotRecord) -> None:
        f = self.path / f"{rec.id}.json"
        try:
            f.write_text(
                json.dumps(asdict(rec), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Не сохранить snapshot %s: %s", rec.id, e)

    def _prune(self) -> None:
        files = sorted(self.path.glob("snap_*.json"))
        if len(files) > MAX_SNAPSHOTS:
            for old in files[:-MAX_SNAPSHOTS]:
                try:
                    old.unlink()
                except OSError:
                    pass

    # ═══════════════════════════════════════════════════════
    # Read
    # ═══════════════════════════════════════════════════════
    def _load(self) -> None:
        if self._loaded:
            return
        self._cache = []
        for f in sorted(self.path.glob("snap_*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                self._cache.append(SnapshotRecord(**data))
            except Exception as e:
                logger.debug("Не прочитать %s: %s", f, e)
        self._loaded = True

    def list(self, limit: int = 20) -> list[SnapshotRecord]:
        self._load()
        return self._cache[-limit:][::-1]

    def get(self, snapshot_id: str) -> SnapshotRecord | None:
        self._load()
        for r in self._cache:
            if r.id == snapshot_id:
                return r
        return None

    def latest(self, slot: str | None = None) -> SnapshotRecord | None:
        self._load()
        if slot:
            for r in reversed(self._cache):
                if r.slot == slot:
                    return r
            return None
        return self._cache[-1] if self._cache else None

    def current_slot(self) -> str:
        last = self.latest()
        return last.slot if last else "a"

    def other_slot(self) -> str:
        return "b" if self.current_slot() == "a" else "a"

    # ═══════════════════════════════════════════════════════
    # Diff
    # ═══════════════════════════════════════════════════════
    def diff(self, from_id: str, to_id: str) -> SnapshotDiff:
        a = self.get(from_id)
        b = self.get(to_id)
        if not a or not b:
            return SnapshotDiff(from_id=from_id, to_id=to_id)

        d = SnapshotDiff(from_id=from_id, to_id=to_id)

        # Agents
        a_keys = set(a.agents.keys())
        b_keys = set(b.agents.keys())
        d.agents_added = sorted(b_keys - a_keys)
        d.agents_removed = sorted(a_keys - b_keys)
        for aid in sorted(a_keys & b_keys):
            if a.agents[aid] != b.agents[aid]:
                d.agents_status_changed.append({
                    "agent": aid,
                    "from": a.agents[aid],
                    "to": b.agents[aid],
                })

        # MCP
        all_mcp = set(a.mcp.keys()) | set(b.mcp.keys())
        for mid in sorted(all_mcp):
            av, bv = a.mcp.get(mid), b.mcp.get(mid)
            if av != bv:
                d.mcp_changed.append({"mcp": mid, "from": av, "to": bv})

        # Capabilities
        all_cap = set(a.capabilities.keys()) | set(b.capabilities.keys())
        for cid in sorted(all_cap):
            av, bv = a.capabilities.get(cid), b.capabilities.get(cid)
            if av != bv:
                d.capabilities_changed.append({
                    "capability": cid, "from": av, "to": bv,
                })

        d.prompt_changed = a.prompt_hash != b.prompt_hash
        return d
