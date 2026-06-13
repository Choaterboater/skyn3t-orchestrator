from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("skyn3t.cortex.proposals")

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class _AutoTriageDecision:
    action: str
    reason: Optional[str] = None

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
    triage_decision: Optional[str] = None
    triage_reason: Optional[str] = None

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
        self._recent_fingerprints: Dict[str, Tuple[float, str]] = {}

    # registration ---
    def register_handler(self, kind: str, fn: ApplyFn) -> None:
        self._handlers[kind] = fn

    def registered_handlers(self) -> List[str]:
        return sorted(self._handlers)

    def counts(self) -> Dict[str, int]:
        """Return ``{status: count}`` across every proposal on disk.

        Cheap to call (one directory scan); intended for the cortex
        status endpoint so operators can see at a glance how the
        proposal queue is moving.
        """
        out: Dict[str, int] = {}
        for p in self.list():
            out[p.status] = out.get(p.status, 0) + 1
        return out

    def recent_failures(self, limit: int = 5) -> List[Dict[str, Any]]:
        """Return the N most recent failed proposals as compact dicts.

        Sorted by decided-at (or created-at fallback), newest first.
        Each entry has ``id``, ``kind``, ``title``, ``error`` — enough
        for an at-a-glance failure inspector in the cortex dashboard
        without dumping the full proposal body.
        """
        failed = self.list(status="failed")
        failed.sort(
            key=lambda p: (p.decided_at or p.created_at or 0.0),
            reverse=True,
        )
        return [
            {
                "id": p.id,
                "kind": p.kind,
                "title": p.title,
                "error": p.error,
            }
            for p in failed[: max(0, int(limit))]
        ]

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

    @staticmethod
    def _has_running_loop() -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        return True

    # CRUD ---
    def create(self, *, kind: str, title: str, summary: str, detail: str,
                payload: Dict[str, Any] | None = None, source: str = "",
               requires_approval: bool = True, origin: str = "system",
               force_requires_approval: bool = False,
               auto_triage_eligible: bool = False) -> Proposal:
        payload_data = dict(payload or {})
        if auto_triage_eligible:
            payload_data.setdefault("_auto_triage_eligible", True)
        triage = self._decide_auto_triage(
            kind=kind,
            title=title,
            summary=summary,
            payload=payload_data,
            source=source,
            requires_approval=requires_approval,
            origin=origin,
            force_requires_approval=force_requires_approval,
            auto_triage_eligible=auto_triage_eligible,
            exclude_id=None,
        )
        triage_decision = triage.action if triage.action != "pending" else None
        triage_reason = triage.reason
        if triage.action == "auto_approved":
            requires_approval = False
        defer_auto_apply = False
        rejected_immediately = triage.action == "auto_rejected"
        decided_now = rejected_immediately
        if not requires_approval and not self._has_running_loop():
            logger.debug(
               "proposal %s/%s requested auto-apply without a running event loop; "
               "storing it as approved so resume_inflight() can replay it later",
               kind,
               title[:80],
            )
            defer_auto_apply = True
            decided_now = True
        elif not requires_approval and self._handlers.get(kind) is None:
            # Handler not registered yet (boot ordering: a component may create
            # an auto-approved proposal before cortex install_handlers runs).
            # Store as approved so resume_inflight() applies it once the handler
            # exists, instead of auto-applying now and hard-failing on "no
            # handler".
            logger.debug(
                "proposal %s/%s auto-apply deferred: no handler for '%s' yet",
                kind,
                title[:80],
                kind,
            )
            defer_auto_apply = True
            decided_now = True
        if rejected_immediately:
            initial_status = "rejected"
        elif defer_auto_apply:
            initial_status = "approved"
        else:
            initial_status = "pending"
        decided_at = time.time() if decided_now else None
        p = Proposal(kind=kind, title=title, summary=summary, detail=detail,
                    payload=payload_data, source=source,
                    status=initial_status, decided_at=decided_at,
                    requires_approval=requires_approval, origin=origin,
                    error=triage.reason if rejected_immediately else None,
                    triage_decision=triage_decision, triage_reason=triage_reason)
        self._write(p, decided=decided_now)
        self._remember_fingerprint(p)
        self._emit({"type": "created", "proposal": p.to_public()})
        if rejected_immediately:
            self._emit({"type": "rejected", "proposal": p.to_public()})
            return p
        if defer_auto_apply:
            self._emit({"type": "approved", "proposal": p.to_public()})
        # User-initiated proposals (requires_approval=False) skip the modal and apply immediately.
        if not requires_approval and not defer_auto_apply:
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
            available = self.registered_handlers()
            p.error = (
                f"no handler for kind '{p.kind}'"
                + (f" (available: {', '.join(available)})" if available else " (available: none)")
            )
            self._move_decided(p)
            self._emit({"type": "failed", "proposal": p.to_public()})
            logger.error("proposal %s failed approval: %s", p.id, p.error)
            return {"ok": False, "error": p.error, "available_handlers": available}

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
        self._remember_fingerprint(p)
        self._emit({"type": "rejected", "proposal": p.to_public()})
        return {"ok": True}

    async def retriage_pending(
        self, *, max_self_edits: Optional[int] = None
    ) -> Dict[str, int]:
        auto_approved = 0
        auto_rejected = 0
        self_edits_done = 0
        # Process every pending origin (system + user dashboard ideas). The
        # per-kind triage gates decide what's eligible; user self-edits only
        # auto-approve when SKYN3T_AUTO_APPLY_SELF_EDITS is on.
        #
        # ``max_self_edits`` bounds how many feature/code_patch applies we
        # spawn per call — each runs the full test suite (up to a 20-min
        # timeout), so draining a deep backlog all at once would peg the box.
        # The remainder stays pending and drains on later cycles.
        for proposal in list(self.list(status="pending")):
            current = self.get(proposal.id)
            if current is None or current.status != "pending":
                continue
            is_self_edit = (
                current.kind in {"feature", "code_patch"}
                and str((current.payload or {}).get("kind") or "").strip()
                != "build_pattern_bias"
            )
            if (
                is_self_edit
                and max_self_edits is not None
                and self_edits_done >= max_self_edits
            ):
                continue
            triage = self._decide_auto_triage(
                kind=current.kind,
                title=current.title,
                summary=current.summary,
                payload=dict(current.payload or {}),
                source=current.source,
                requires_approval=current.requires_approval,
                origin=current.origin,
                force_requires_approval=False,
                auto_triage_eligible=self._proposal_auto_triage_eligible(current),
                exclude_id=current.id,
            )
            if triage.action == "auto_rejected":
                current.status = "rejected"
                current.decided_at = time.time()
                current.error = triage.reason
                current.triage_decision = "auto_rejected"
                current.triage_reason = triage.reason
                self._move_decided(current)
                self._emit({"type": "rejected", "proposal": current.to_public()})
                auto_rejected += 1
                continue
            if triage.action == "auto_approved":
                # Don't approve a kind whose handler isn't registered yet
                # (boot ordering: retriage can run before install_handlers).
                # Approving now would hard-fail it; leave it pending so a later
                # sweep approves it once the handler exists.
                if self._handlers.get(current.kind) is None:
                    continue
                current.requires_approval = False
                current.triage_decision = "auto_approved"
                current.triage_reason = triage.reason
                self._write(current, decided=False)
                await self.approve(current.id)
                auto_approved += 1
                if is_self_edit:
                    self_edits_done += 1
        return {
            "auto_approved": auto_approved,
            "auto_rejected": auto_rejected,
        }

    async def resume_inflight(self) -> Dict[str, int]:
        requeued = 0
        failed = 0
        for proposal in self.list():
            if proposal.status not in {"approved", "applying"} or proposal.applied_at is not None:
                continue
            handler = self._handlers.get(proposal.kind)
            if handler is None:
                proposal.status = "failed"
                available = self.registered_handlers()
                proposal.error = (
                    f"no handler for kind '{proposal.kind}'"
                    + (f" (available: {', '.join(available)})" if available else " (available: none)")
                )
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

    def inflight_apply_count(self) -> int:
        """How many proposal applies are running in the background right now."""
        return len(self._active_apply_ids)

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
        self._remember_fingerprint(p)

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
            available = self.registered_handlers()
            p.error = (
                f"no handler for kind '{p.kind}'"
                + (f" (available: {', '.join(available)})" if available else " (available: none)")
            )
            self._write(p, decided=True)
            self._emit({"type": "failed", "proposal": p.to_public()})
            logger.error("proposal %s apply failed: %s", p.id, p.error)
            return
        try:
            if p.status != "applying":
                p.status = "applying"
                self._write(p, decided=True)
                self._emit({"type": "applying", "proposal": p.to_public()})
            payload = dict(p.payload or {})
            payload.setdefault("_proposal_id", p.id)
            payload.setdefault("_proposal_kind", p.kind)
            payload.setdefault("_proposal_source", p.source)
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

    def _decide_auto_triage(
        self,
        *,
        kind: str,
        title: str,
        summary: str,
        payload: Dict[str, Any],
        source: str,
        requires_approval: bool,
        origin: str,
        force_requires_approval: bool,
        auto_triage_eligible: bool,
        exclude_id: Optional[str] = None,
    ) -> _AutoTriageDecision:
        try:
            from skyn3t.config.settings import auto_approve_enabled, get_settings

            settings = get_settings()
            full_auto = auto_approve_enabled(settings)
        except Exception:
            return _AutoTriageDecision("pending")

        # Operator opt-in (SKYN3T_AUTO_APPLY_SELF_EDITS=1) to let the system
        # apply its own learnings. Safe because code_improver applies every
        # patch on a throwaway `skyn3t/auto/<id>` branch behind a pytest gate —
        # main is never edited directly.
        self_edits_auto = full_auto and bool(
            getattr(settings, "auto_apply_self_edits", False)
        )

        # Self-edit proposals (feature / code_patch), including user-submitted
        # dashboard ideas (origin != "system"), auto-approve when opted in.
        # build_pattern_bias is handled by its own safe branch further down.
        if (
            self_edits_auto
            and kind in {"feature", "code_patch"}
            and str(payload.get("kind") or "").strip() != "build_pattern_bias"
        ):
            return _AutoTriageDecision(
                "auto_approved",
                "auto-approved self-edit (auto-apply mode)",
            )

        # Safety floor: never auto-approve SkyN3t repo self-edits unless the
        # operator explicitly opted in above.
        if kind == "code_patch":
            return _AutoTriageDecision("pending")
        if kind == "feature" and str(payload.get("kind") or "").strip() != "build_pattern_bias":
            return _AutoTriageDecision("pending")

        if not requires_approval or origin != "system":
            return _AutoTriageDecision("pending")
        if force_requires_approval and not full_auto:
            return _AutoTriageDecision("pending")

        if full_auto:
            if kind == "ingest":
                topic = str(payload.get("topic") or payload.get("query") or title or "").strip()
                repo = str(payload.get("repo") or "").strip()
                if topic or repo:
                    return _AutoTriageDecision(
                        "auto_approved",
                        "auto-approved ingest (no-approval mode)",
                    )
            if kind == "tuning":
                return _AutoTriageDecision(
                    "auto_approved",
                    "auto-approved tuning (no-approval mode)",
                )

        if not getattr(settings, "cortex_auto_approve_system", False):
            return _AutoTriageDecision("pending")

        fingerprint = self._fingerprint_for_fields(
            kind=kind,
            title=title,
            summary=summary,
            payload=payload,
            source=source,
        )
        if (
            kind in {"feature", "ingest"}
            and getattr(settings, "cortex_auto_reject_duplicates", True)
            and fingerprint
        ):
            duplicate_id = self._find_recent_duplicate(
                fingerprint,
                window_seconds=max(
                    0,
                    int(getattr(settings, "cortex_auto_triage_duplicate_window_seconds", 86_400)),
                ),
                exclude_id=exclude_id,
            )
            if duplicate_id is not None:
                return _AutoTriageDecision(
                    "auto_rejected",
                    f"auto-rejected duplicate of {duplicate_id}",
                )

        if kind == "ingest":
            if getattr(settings, "cortex_auto_reject_low_signal_ingest", True):
                low_signal_reason = self._low_signal_ingest_reason(
                    title=title,
                    summary=summary,
                    payload=payload,
                    min_topic_length=max(
                        1,
                        int(getattr(settings, "cortex_auto_triage_min_ingest_topic_length", 6)),
                    ),
                )
                if low_signal_reason is not None:
                    return _AutoTriageDecision("auto_rejected", low_signal_reason)

            if (
                getattr(settings, "cortex_auto_approve_scout_ingest", True)
                and self._is_safe_auto_approve_scout_ingest(
                    payload,
                    source=source,
                    max_limit=max(
                        1,
                        int(getattr(settings, "cortex_auto_triage_max_scout_ingest_limit", 10)),
                    ),
                )
            ):
                return _AutoTriageDecision(
                    "auto_approved",
                    "auto-approved GitHub scout ingest",
                )

            if (
                auto_triage_eligible
                and getattr(settings, "cortex_auto_approve_safe_ingest", True)
                and self._is_safe_auto_approve_ingest(
                    payload,
                    max_limit=max(
                        1,
                        int(getattr(settings, "cortex_auto_triage_max_safe_ingest_limit", 3)),
                    ),
                )
            ):
                return _AutoTriageDecision(
                    "auto_approved",
                    "auto-approved safe ingest",
                )

        if kind == "studio_debug":
            invalid_studio_debug_reason = self._invalid_studio_debug_reason(payload)
            if invalid_studio_debug_reason is not None:
                return _AutoTriageDecision("auto_rejected", invalid_studio_debug_reason)

        if (
            kind == "tuning"
            and getattr(settings, "cortex_auto_approve_safe_tuning", True)
            and self._is_safe_auto_approve_tuning(payload)
        ):
            return _AutoTriageDecision(
                "auto_approved",
                "auto-approved safe agent tuning",
            )

        if (
            kind == "feature"
            and str(payload.get("kind") or "").strip() == "build_pattern_bias"
            and getattr(settings, "cortex_auto_approve_build_pattern_bias", True)
            and self._is_safe_auto_approve_build_pattern_bias(payload)
        ):
            return _AutoTriageDecision(
                "auto_approved",
                "auto-approved build pattern bias",
            )

        return _AutoTriageDecision("pending")

    def _low_signal_ingest_reason(
        self,
        *,
        title: str,
        summary: str,
        payload: Dict[str, Any],
        min_topic_length: int,
    ) -> Optional[str]:
        topic = str(payload.get("topic") or "").strip()
        candidate = topic or title or summary
        normalized = self._normalize_subject(candidate)
        if not normalized:
            return "auto-rejected low-signal ingest: empty topic"
        if topic and len(normalized) < min_topic_length:
            return "auto-rejected low-signal ingest: topic too short"
        return None

    def _is_safe_auto_approve_ingest(self, payload: Dict[str, Any], *, max_limit: int) -> bool:
        topic = self._normalize_subject(payload.get("topic"))
        mode = str(payload.get("mode") or "").strip().lower()
        raw_limit = payload.get("limit", 0)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return False
        return bool(topic) and mode == "search" and 0 < limit <= max_limit

    @staticmethod
    def _is_safe_auto_approve_scout_ingest(
        payload: Dict[str, Any],
        *,
        source: str,
        max_limit: int,
    ) -> bool:
        if not str(source or "").strip().lower().startswith("repo_scout:github"):
            return False
        repo = str(payload.get("repo") or "").strip()
        if not repo or "/" not in repo or repo.count("/") != 1:
            return False
        reuse_risk = str(payload.get("reuse_risk") or "").lower()
        if reuse_risk == "high":
            return False
        raw_limit = payload.get("limit", 8)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return False
        return 0 < limit <= max_limit

    @staticmethod
    def _is_safe_auto_approve_tuning(payload: Dict[str, Any]) -> bool:
        adjustments = payload.get("adjustments") or []
        if not isinstance(adjustments, list) or not adjustments:
            return False
        safe = {"request_interval", "timeout", "max_tokens", "prompt_suffix", "auth_retry"}
        for adj in adjustments:
            if not isinstance(adj, dict):
                return False
            if str(adj.get("parameter") or "").strip() not in safe:
                return False
        return True

    @staticmethod
    def _is_safe_auto_approve_build_pattern_bias(payload: Dict[str, Any]) -> bool:
        stack = str(payload.get("stack") or "").strip()
        winner_shape = payload.get("winner_shape") or []
        if not stack:
            return False
        if not isinstance(winner_shape, list) or not winner_shape:
            return False
        return all(str(path).strip() for path in winner_shape)

    def _invalid_studio_debug_reason(self, payload: Dict[str, Any]) -> Optional[str]:
        raw_target = payload.get("target_file")
        target = "" if raw_target is None else str(raw_target).strip()
        if not target or target.lower() in {"none", "null"}:
            return "auto-rejected invalid studio_debug: missing target_file"
        return None

    def _proposal_auto_triage_eligible(self, proposal: Proposal) -> bool:
        payload = proposal.payload or {}
        if bool(payload.get("_auto_triage_eligible")):
            return True
        return proposal.kind == "ingest" and proposal.source == "explorer"

    def _find_recent_duplicate(
        self,
        fingerprint: str,
        *,
        window_seconds: int,
        exclude_id: Optional[str] = None,
    ) -> Optional[str]:
        if not fingerprint or window_seconds <= 0:
            return None
        cutoff = time.time() - window_seconds
        self._prune_recent_fingerprints(cutoff)
        cached = self._recent_fingerprints.get(fingerprint)
        if cached is not None and cached[0] >= cutoff and cached[1] != exclude_id:
            return cached[1]
        for proposal in self.list(origin="system"):
            if exclude_id is not None and proposal.id == exclude_id:
                continue
            if proposal.status == "failed":
                continue
            if self._recency_timestamp(proposal) < cutoff:
                continue
            other = self._fingerprint_for_fields(
                kind=proposal.kind,
                title=proposal.title,
                summary=proposal.summary,
                payload=proposal.payload,
                source=proposal.source,
            )
            if other == fingerprint:
                self._recent_fingerprints[fingerprint] = (
                    self._recency_timestamp(proposal),
                    proposal.id,
                )
                return proposal.id
        return None

    def _remember_fingerprint(self, proposal: Proposal) -> None:
        fingerprint = self._fingerprint_for_fields(
            kind=proposal.kind,
            title=proposal.title,
            summary=proposal.summary,
            payload=proposal.payload,
            source=proposal.source,
        )
        if not fingerprint:
            return
        self._recent_fingerprints[fingerprint] = (
            self._recency_timestamp(proposal),
            proposal.id,
        )

    def _prune_recent_fingerprints(self, cutoff: float) -> None:
        for fingerprint, (ts, _) in list(self._recent_fingerprints.items()):
            if ts < cutoff:
                self._recent_fingerprints.pop(fingerprint, None)

    def _fingerprint_for_fields(
        self,
        *,
        kind: str,
        title: str,
        summary: str,
        payload: Dict[str, Any],
        source: str,
    ) -> str:
        subject_parts: List[str] = []
        if kind == "ingest":
            subject_parts.extend(
                [
                    str(payload.get("repo_key") or ""),
                    str(payload.get("repo") or ""),
                    str(payload.get("topic") or ""),
                    str(payload.get("query") or ""),
                ]
            )
        elif kind == "feature":
            subject_parts.extend(
                [
                    str(payload.get("kind") or ""),
                    str(payload.get("stack") or ""),
                    str(payload.get("target_file") or ""),
                    str(payload.get("idea") or ""),
                    title,
                    summary,
                ]
            )
        else:
            subject_parts.extend(
                [
                    str(payload.get("target_file") or ""),
                    str(payload.get("agent") or ""),
                    str(payload.get("topic") or ""),
                    title,
                    summary,
                ]
            )
        normalized_parts = [
            self._normalize_subject(part)
            for part in [kind, source, *subject_parts]
            if self._normalize_subject(part)
        ]
        return "::".join(normalized_parts)

    def _normalize_subject(self, value: Any) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        return _NORMALIZE_RE.sub(" ", text).strip()

    def _recency_timestamp(self, proposal: Proposal) -> float:
        return float(proposal.decided_at or proposal.created_at or 0.0)

# module-level singleton, lazy
_store: Optional[ProposalStore] = None
def get_store() -> ProposalStore:
    global _store
    if _store is None:
        _store = ProposalStore()
    return _store
