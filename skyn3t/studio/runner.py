"""High-level Project Studio orchestrator.

:class:`StudioRunner` takes a free-form brief plus a template key and
runs the corresponding pipeline of specialist agents, persisting a
project manifest and any artifacts they produce under ``projects/``.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import AgentCapability, TaskRequest
from skyn3t.studio.registry import get_agent
from skyn3t.studio.templates import get_template


class StudioRunner:
    """Run project templates end-to-end against a pool of specialist agents."""

    def __init__(
        self,
        *,
        event_bus: Any,
        rag: Any = None,
        projects_root: Path = Path("projects"),
    ) -> None:
        self.event_bus = event_bus
        self.rag = rag
        self.projects_root = Path(projects_root)
        self.projects_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def start(
        self,
        template_key: str,
        brief: str,
        *,
        slug: Optional[str] = None,
        extra: Optional[dict] = None,
    ) -> dict:
        """Execute the named template against ``brief`` and return the manifest."""
        template = get_template(template_key)
        slug = slug or self._slugify(brief or template_key)
        artifact_dir = self.projects_root / slug
        artifact_dir.mkdir(parents=True, exist_ok=True)

        manifest: Dict[str, Any] = {
            "slug": slug,
            "template": template_key,
            "title": template.title,
            "brief": brief,
            "stages": [],
            "artifacts": [],
            "started_at": time.time(),
        }
        (artifact_dir / "project.json").write_text(json.dumps(manifest, indent=2))

        self._publish(
            "PROJECT_STARTED",
            {"slug": slug, "template": template_key, "title": template.title},
        )

        # Auto-detect target_file from the brief (e.g. "target_file: skyn3t/web/dashboard.html").
        # Also infer from common phrasings so users don't have to type the keyword.
        import re
        auto_target = None
        m = re.search(r"target_file\s*[:=]\s*([^\s]+)", brief or "")
        if m:
            auto_target = m.group(1).strip().rstrip(".,")
        else:
            # fallback: any path-looking token containing a known repo path prefix
            m2 = re.search(r"\b(skyn3t/\S+\.\w+)", brief or "")
            if m2:
                auto_target = m2.group(1).strip().rstrip(".,")
        if auto_target:
            extra = dict(extra or {})
            extra.setdefault("target_file", auto_target)
            extra.setdefault("rationale", brief)

        results: list = []
        for stage in template.stages:
            agent = get_agent(stage.agent, event_bus=self.event_bus, rag=self.rag)
            if hasattr(agent, "initialize"):
                maybe = agent.initialize()
                if hasattr(maybe, "__await__"):
                    await maybe

            input_data: Dict[str, Any] = {
                "brief": brief,
                "artifact_dir": str(artifact_dir),
                "next_agent": stage.handoff_to,
                **(extra or {}),
                **stage.input_extra,
            }
            task = TaskRequest(
                title=f"{template_key}:{stage.name}",
                input_data=input_data,
            )
            # ``required_capability`` is not part of the base TaskRequest
            # dataclass, but downstream agents/orchestrators may consult it
            # for routing; attach it dynamically so the information is not
            # lost without breaking instantiation.
            if "required_capability" in TaskRequest.__dataclass_fields__:
                setattr(
                    task,
                    "required_capability",
                    AgentCapability(name=stage.capability),
                )
            else:
                task.input_data.setdefault(
                    "required_capability", stage.capability
                )

            self._publish(
                "PROJECT_STAGE_STARTED",
                {"slug": slug, "stage": stage.name, "agent": stage.agent},
            )
            try:
                result = await agent.execute(task)
            except Exception as e:  # noqa: BLE001 - surface failure into manifest
                self._publish(
                    "PROJECT_STAGE_FAILED",
                    {"slug": slug, "stage": stage.name, "error": str(e)},
                )
                manifest["stages"].append(
                    {
                        "name": stage.name,
                        "agent": stage.agent,
                        "ok": False,
                        "error": str(e),
                    }
                )
                (artifact_dir / "project.json").write_text(
                    json.dumps(manifest, indent=2)
                )
                break

            ok = bool(getattr(result, "success", True))
            output = getattr(result, "output", None) or {}
            manifest["stages"].append(
                {
                    "name": stage.name,
                    "agent": stage.agent,
                    "ok": ok,
                    "output": output,
                }
            )
            for f in (output or {}).get("files", []) if isinstance(output, dict) else []:
                if f not in manifest["artifacts"]:
                    manifest["artifacts"].append(f)
            results.append(result)
            self._publish(
                "PROJECT_STAGE_COMPLETED",
                {"slug": slug, "stage": stage.name},
            )
            if not ok:
                break

        manifest["completed_at"] = time.time()
        manifest["artifacts"] = self._scan_artifacts(artifact_dir)
        (artifact_dir / "project.json").write_text(json.dumps(manifest, indent=2))
        self._publish(
            "PROJECT_COMPLETED",
            {
                "slug": slug,
                "template": template_key,
                "title": template.title,
                "stages_completed": len(results),
            },
        )
        return manifest

    def list_projects(self) -> List[dict]:
        """Return every project manifest under ``projects_root``."""
        out: List[dict] = []
        if not self.projects_root.exists():
            return out
        for p in sorted(self.projects_root.iterdir()):
            if not p.is_dir():
                continue
            mf = p / "project.json"
            if mf.exists():
                try:
                    out.append(json.loads(mf.read_text()))
                except Exception:
                    continue
        return out

    def get_project(self, slug: str) -> Optional[dict]:
        """Return the manifest for ``slug`` or ``None`` if it does not exist."""
        mf = self.projects_root / slug / "project.json"
        if not mf.exists():
            return None
        try:
            return json.loads(mf.read_text())
        except Exception:
            return None

    def export_zip(self, slug: str) -> Path:
        """Bundle a project directory into a zip file and return its path."""
        import shutil

        src = self.projects_root / slug
        if not src.exists():
            raise FileNotFoundError(slug)
        zip_path = self.projects_root / f"{slug}.zip"
        if zip_path.exists():
            zip_path.unlink()
        base = str(zip_path)
        if base.endswith(".zip"):
            base = base[:-4]
        shutil.make_archive(base, "zip", src)
        return zip_path

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _slugify(s: str) -> str:
        s = (s or "").lower().strip()
        # Collapse any run of non-alnum characters to a single dash.
        s = re.sub(r"[^a-z0-9]+", "-", s)
        s = s.strip("-")
        if not s:
            s = "project"
        s = s[:60].rstrip("-")
        suffix = f"{time.time_ns():x}"[-6:]
        return f"{s}-{suffix}"

    def _scan_artifacts(self, d: Path) -> List[str]:
        """Return relative paths of files under ``d`` (capped at 200)."""
        if not d.exists():
            return []
        entries: List[str] = []
        for p in sorted(d.rglob("*")):
            if p.is_file():
                try:
                    rel = p.relative_to(d).as_posix()
                except ValueError:
                    rel = p.name
                entries.append(rel)
                if len(entries) >= 200:
                    break
        return entries

    def _publish(self, name: str, payload: dict) -> None:
        """Publish a project event onto the shared event bus.

        We ride the generic ``SYSTEM_ALERT`` event type so no new
        ``EventType`` member is required - the dashboard distinguishes
        project events via the ``kind`` field on the payload.
        """
        try:
            from skyn3t.core.events import Event, EventType

            self.event_bus.publish(
                Event(
                    event_type=EventType.SYSTEM_ALERT,
                    source="studio",
                    payload={"kind": name, **payload},
                )
            )
        except Exception:
            pass
