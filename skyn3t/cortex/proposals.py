from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
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
    status: str = "pending"        # pending | approved | applying | rejected | applied | failed
    created_at: float = field(default_factory=time.time)
    decided_at: Optional[float] = None
    applied_at: Optional[float] = None
    error: Optional[str] = None
    requires_approval: bool = True   # False → auto-apply on creation (user-initiated)
    origin: str = "system"           # "system" | "user"

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
        self._apply_tasks: set[asyncio.Task] = set()
        self._active_apply_ids: set[str] = set()

    # registration ---
    def register_handler(self, kind: str, fn: ApplyFn) -> None:
        self._handlers[kind] = fn

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._listeners.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._listeners.remove(q)
        except ValueError:
            pass

    def _emit(self, event: Dict[str, Any]) -> None:
        for q in list(self._listeners):
            try:
                q.put_nowait(event)
            except Exception:
                try:
                    self._listeners.remove(q)
                except ValueError:
                    pass

    # CRUD ---
    def create(self, *, kind: str, title: str, summary: str, detail: str,
                payload: Dict[str, Any] | None = None, source: str = "",
               requires_approval: bool = True, origin: str = "system") -> Proposal:
        p = Proposal(kind=kind, title=title, summary=summary, detail=detail,
                     payload=payload or {}, source=source,
                     requires_approval=requires_approval, origin=origin)
        self._write(p)
        self._emit({"type": "created", "proposal": p.to_public()})
        # User-initiated proposals (requires_approval=False) skip the modal and apply immediately.
        if not requires_approval:
            asyncio.create_task(self._auto_apply(p.id))
        return p

    async def _auto_apply(self, pid: str) -> None:
        try:
            await self.approve(pid)
        except Exception:
            logger.exception("auto-apply failed for %s", pid)

    def list(self, status: Optional[str] = None, origin: Optional[str] = None) -> List[Proposal]:
        out: List[Proposal] = []
        origin_filter = str(origin or "").strip().lower() or None
        for sub in ("pending", "decided"):
            for f in (self.root / sub).glob("*.json"):
                try:
                    raw = json.loads(f.read_text())
                    p = Proposal(**raw)
                    proposal_origin = str(getattr(p, "origin", "system") or "system").lower()
                    if origin_filter is not None and proposal_origin != origin_filter:
                        continue
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
                try:
                    return Proposal(**json.loads(f.read_text()))
                except Exception:
                    logger.debug("proposal %s parse failed at %s", pid, f, exc_info=True)
        return None

    async def approve(self, pid: str) -> Dict[str, Any]:
        p = self.get(pid)
        if p is None or p.status != "pending":
            return {"ok": False, "error": "not pending"}
        p.decided_at = time.time()

        handler = self._handlers.get(p.kind)
        if handler is None:
            p.status = "failed"
            p.error = "no handler for kind"
            self._move_decided(p)
            self._emit({"type": "failed", "proposal": p.to_public()})
            return {"ok": False, "error": p.error}

        p.status = "approved"
        self._move_decided(p)
        self._emit({"type": "approved", "proposal": p.to_public()})
        self._spawn_apply(pid)
        return {"ok": True, "applied": False, "status": "approved"}

    def reject(self, pid: str, reason: str = "") -> Dict[str, Any]:
        p = self.get(pid)
        if p is None or p.status != "pending":
            return {"ok": False, "error": "not pending"}
        p.status = "rejected"
        p.decided_at = time.time()
        if reason:
            p.error = reason
        self._move_decided(p)
        self._emit({"type": "rejected", "proposal": p.to_public()})
        return {"ok": True}

    async def resume_inflight(self) -> Dict[str, int]:
        requeued = 0
        failed = 0
        for proposal in self.list():
            if proposal.status not in {"approved", "applying"} or proposal.applied_at is not None:
                continue
            handler = self._handlers.get(proposal.kind)
            if handler is None:
                proposal.status = "failed"
                proposal.error = "no handler for kind"
                self._write(proposal, decided=True)
                self._emit({"type": "failed", "proposal": proposal.to_public()})
                failed += 1
                continue
            if self._spawn_apply(proposal.id):
                requeued += 1
        return {"requeued": requeued, "failed_no_handler": failed}

    async def cancel_inflight(self) -> Dict[str, int]:
        tasks = list(self._apply_tasks)
        if not tasks:
            return {"cancelled": 0}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return {"cancelled": len(tasks)}

    # internals ---
    def _path(self, p: Proposal, *, decided: bool = False) -> Path:
        return self.root / ("decided" if decided else "pending") / f"{p.id}.json"

    def _write(self, p: Proposal, *, decided: bool = False) -> None:
        self._path(p, decided=decided).write_text(json.dumps(p.to_public(), indent=2))

    def _move_decided(self, p: Proposal) -> None:
        old = self._path(p, decided=False)
        if old.exists():
            old.unlink()
        self._write(p, decided=True)

    def _spawn_apply(self, pid: str) -> bool:
        if pid in self._active_apply_ids:
            return False
        self._active_apply_ids.add(pid)
        task = asyncio.create_task(self._apply(pid))
        self._apply_tasks.add(task)
        task.add_done_callback(self._finish_apply_task_callback(pid))
        return True

    def _finish_apply_task_callback(self, pid: str) -> Callable[[asyncio.Task], None]:
        def _callback(task: asyncio.Task) -> None:
            self._finish_apply_task(pid, task)

        return _callback

    def _finish_apply_task(self, pid: str, task: asyncio.Task) -> None:
        self._active_apply_ids.discard(pid)
        self._apply_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("background apply failed for %s", pid)

    async def _apply(self, pid: str) -> None:
        p = self.get(pid)
        if p is None:
            return
        handler = self._handlers.get(p.kind)
        if handler is None:
            p.status = "failed"
            p.error = "no handler for kind"
            self._write(p, decided=True)
            self._emit({"type": "failed", "proposal": p.to_public()})
            return
        try:
            if p.status != "applying":
                p.status = "applying"
                self._write(p, decided=True)
                self._emit({"type": "applying", "proposal": p.to_public()})
            payload = dict(p.payload or {})
            payload.setdefault("_proposal_id", p.id)
            payload.setdefault("_proposal_kind", p.kind)
            result = await handler(payload)
            ok = True
            if isinstance(result, dict) and "ok" in result:
                ok = bool(result.get("ok"))
            if not ok:
                p.status = "failed"
                p.error = str((result or {}).get("error") or "apply returned ok=false")
                self._write(p, decided=True)
                self._emit({"type": "failed", "proposal": p.to_public(), "result": result})
                return
            p.status = "applied"
            p.applied_at = time.time()
            p.error = None
            self._write(p, decided=True)
            self._emit({"type": "applied", "proposal": p.to_public(), "result": result})
        except Exception as e:
            p.status = "failed"
            p.error = str(e)
            self._write(p, decided=True)
            self._emit({"type": "failed", "proposal": p.to_public()})
            logger.exception("apply failed for %s", pid)

# module-level singleton, lazy
_store: Optional[ProposalStore] = None
def get_store() -> ProposalStore:
    global _store
    if _store is None:
        _store = ProposalStore()
    return _store
