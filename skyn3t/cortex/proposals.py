from __future__ import annotations
import asyncio, json, logging, time, uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("skyn3t.cortex.proposals")

@dataclass
class Proposal:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    kind: str = "generic"          # "tuning" | "exploration" | "code_patch" | "ingest" | "generic"
    title: str = ""
    summary: str = ""
    detail: str = ""               # markdown/diff/plain text shown in popup
    payload: Dict[str, Any] = field(default_factory=dict)
    source: str = ""               # which module proposed it
    status: str = "pending"        # pending | approved | rejected | applied | failed
    created_at: float = field(default_factory=time.time)
    decided_at: Optional[float] = None
    applied_at: Optional[float] = None
    error: Optional[str] = None

    def to_public(self) -> Dict[str, Any]:
        return asdict(self)

ApplyFn = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]

class ProposalStore:
    def __init__(self, root: Path | str = "data/proposals"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "pending").mkdir(exist_ok=True)
        (self.root / "decided").mkdir(exist_ok=True)
        self._handlers: Dict[str, ApplyFn] = {}
        self._listeners: List[asyncio.Queue] = []

    # registration ---
    def register_handler(self, kind: str, fn: ApplyFn) -> None:
        self._handlers[kind] = fn

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._listeners.append(q)
        return q

    def _emit(self, event: Dict[str, Any]) -> None:
        for q in list(self._listeners):
            try: q.put_nowait(event)
            except Exception:
                try: self._listeners.remove(q)
                except ValueError: pass

    # CRUD ---
    def create(self, *, kind: str, title: str, summary: str, detail: str,
               payload: Dict[str, Any] | None = None, source: str = "") -> Proposal:
        p = Proposal(kind=kind, title=title, summary=summary, detail=detail,
                     payload=payload or {}, source=source)
        self._write(p)
        self._emit({"type": "created", "proposal": p.to_public()})
        return p

    def list(self, status: Optional[str] = None) -> List[Proposal]:
        out: List[Proposal] = []
        for sub in ("pending", "decided"):
            for f in (self.root / sub).glob("*.json"):
                try:
                    raw = json.loads(f.read_text())
                    p = Proposal(**raw)
                    if status is None or p.status == status:
                        out.append(p)
                except Exception:
                    logger.exception("could not load %s", f)
        out.sort(key=lambda p: p.created_at, reverse=True)
        return out

    def get(self, pid: str) -> Optional[Proposal]:
        for sub in ("pending", "decided"):
            f = self.root / sub / f"{pid}.json"
            if f.exists():
                try: return Proposal(**json.loads(f.read_text()))
                except Exception: pass
        return None

    async def approve(self, pid: str) -> Dict[str, Any]:
        p = self.get(pid)
        if p is None or p.status != "pending":
            return {"ok": False, "error": "not pending"}
        p.status = "approved"; p.decided_at = time.time()
        self._move_decided(p)
        self._emit({"type": "approved", "proposal": p.to_public()})

        handler = self._handlers.get(p.kind)
        if handler is None:
            return {"ok": True, "applied": False, "reason": "no handler for kind"}
        try:
            result = await handler(p.payload)
            p.status = "applied"; p.applied_at = time.time()
            self._write(p, decided=True)
            self._emit({"type": "applied", "proposal": p.to_public(), "result": result})
            return {"ok": True, "applied": True, "result": result}
        except Exception as e:
            p.status = "failed"; p.error = str(e)
            self._write(p, decided=True)
            self._emit({"type": "failed", "proposal": p.to_public()})
            logger.exception("apply failed for %s", pid)
            return {"ok": False, "error": str(e)}

    def reject(self, pid: str, reason: str = "") -> Dict[str, Any]:
        p = self.get(pid)
        if p is None or p.status != "pending":
            return {"ok": False, "error": "not pending"}
        p.status = "rejected"; p.decided_at = time.time()
        if reason: p.error = reason
        self._move_decided(p)
        self._emit({"type": "rejected", "proposal": p.to_public()})
        return {"ok": True}

    # internals ---
    def _path(self, p: Proposal, *, decided: bool = False) -> Path:
        return self.root / ("decided" if decided else "pending") / f"{p.id}.json"

    def _write(self, p: Proposal, *, decided: bool = False) -> None:
        self._path(p, decided=decided).write_text(json.dumps(p.to_public(), indent=2))

    def _move_decided(self, p: Proposal) -> None:
        old = self._path(p, decided=False)
        if old.exists(): old.unlink()
        self._write(p, decided=True)

# module-level singleton, lazy
_store: Optional[ProposalStore] = None
def get_store() -> ProposalStore:
    global _store
    if _store is None:
        _store = ProposalStore()
    return _store
