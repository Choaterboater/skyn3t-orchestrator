"""File Operations Agent - reads, writes, searches, organizes, and watches files."""

import fnmatch
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus


class FileOpsAgent(BaseAgent):
    """Agent for file read/write, search, directory organization, and file watching."""

    def __init__(
        self,
        name: str = "file_ops_agent",
        event_bus: EventBus = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="file_ops",
            provider="local",
            event_bus=event_bus,
            config=config,
        )
        self.add_capability(
            AgentCapability(
                name="file_read",
                description="Read file contents safely",
                parameters={"path": "str", "offset": "int", "limit": "int"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="file_write",
                description="Write content to files",
                parameters={"path": "str", "content": "str", "append": "bool"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="file_search",
                description="Search for files by name or content",
                parameters={"pattern": "str", "path": "str", "content_search": "bool"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="directory_organize",
                description="Organize files within directories",
                parameters={"source_dir": "str", "rules": "list"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="file_watch",
                description="Watch files for changes",
                parameters={"paths": "list", "events": "list"},
            )
        )
        self._base_dir = Path(self.config.get("base_dir", os.getcwd())).resolve()
        self._watched_paths: Dict[str, Dict[str, Any]] = {}
        self._max_file_size = self.config.get("max_file_size", 10 * 1024 * 1024)  # 10MB

    async def initialize(self) -> None:
        """Initialize the file operations agent."""
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self.metadata["base_dir"] = str(self._base_dir)
        self.metadata["initialized"] = True
        self.metadata["watched_paths_count"] = 0

    async def health_check(self) -> bool:
        """Check if the file operations environment is healthy."""
        try:
            test_path = self._base_dir / ".health_check"
            test_path.write_text("ok")
            content = test_path.read_text()
            test_path.unlink()
            return content == "ok"
        except Exception:
            return False

    async def execute(self, task: TaskRequest) -> TaskResult:
        """Execute a file operations task."""
        task_type = task.input_data.get("task_type", "file_read")

        handlers = {
            "file_read": self._file_read,
            "file_write": self._file_write,
            "file_search": self._file_search,
            "directory_organize": self._directory_organize,
            "file_watch": self._file_watch,
        }

        handler = handlers.get(task_type)
        if not handler:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=f"Unknown task type: {task_type}",
            )

        try:
            result = await handler(task)
            return TaskResult(
                task_id=task.task_id,
                success=result.get("success", True),
                output=result,
            )
        except Exception as e:
            return TaskResult(
                task_id=task.task_id,
                success=False,
                error=str(e),
            )

    def _resolve_path(self, path: str) -> Path:
        """Resolve a path relative to base_dir, preventing directory traversal."""
        target = (self._base_dir / path).resolve()
        # Security check: ensure resolved path is within base_dir
        try:
            target.relative_to(self._base_dir)
        except ValueError:
            raise ValueError(f"Path '{path}' is outside the allowed base directory")
        return target

    async def _file_read(self, task: TaskRequest) -> Dict[str, Any]:
        """Read a file's contents."""
        path = task.input_data.get("path", "")
        offset = task.input_data.get("offset", 0)
        limit = task.input_data.get("limit", 1000)

        if not path:
            return {"success": False, "error": "No path provided"}

        try:
            target = self._resolve_path(path)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        if not target.exists():
            return {"success": False, "error": f"File not found: {path}"}
        if target.is_dir():
            return {"success": False, "error": f"Path is a directory: {path}"}

        file_size = target.stat().st_size
        if file_size > self._max_file_size:
            return {"success": False, "error": f"File too large: {file_size} bytes (max {self._max_file_size})"}

        try:
            content = target.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            total_lines = len(lines)
            selected_lines = lines[offset:offset + limit]

            return {
                "success": True,
                "path": str(target),
                "content": "\n".join(selected_lines),
                "total_lines": total_lines,
                "offset": offset,
                "lines_returned": len(selected_lines),
                "file_size": file_size,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _file_write(self, task: TaskRequest) -> Dict[str, Any]:
        """Write content to a file."""
        path = task.input_data.get("path", "")
        content = task.input_data.get("content", "")
        append = task.input_data.get("append", False)

        if not path:
            return {"success": False, "error": "No path provided"}

        try:
            target = self._resolve_path(path)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append else "w"
            with open(target, mode, encoding="utf-8") as f:
                f.write(content)

            return {
                "success": True,
                "path": str(target),
                "bytes_written": len(content.encode("utf-8")),
                "mode": "append" if append else "write",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _file_search(self, task: TaskRequest) -> Dict[str, Any]:
        """Search for files by name or content."""
        pattern = task.input_data.get("pattern", "")
        search_path = task.input_data.get("path", ".")
        content_search = task.input_data.get("content_search", False)
        max_results = task.input_data.get("max_results", 100)

        if not pattern:
            return {"success": False, "error": "No search pattern provided"}

        try:
            target_dir = self._resolve_path(search_path)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        if not target_dir.exists():
            return {"success": False, "error": f"Directory not found: {search_path}"}

        matches = []
        try:
            for root, dirs, files in os.walk(target_dir):
                # Respect max_results
                if len(matches) >= max_results:
                    break

                for filename in files:
                    if len(matches) >= max_results:
                        break

                    full_path = Path(root) / filename
                    rel_path = full_path.relative_to(self._base_dir)

                    # Name-based search
                    if fnmatch.fnmatch(filename.lower(), pattern.lower()) or fnmatch.fnmatch(filename.lower(), f"*{pattern.lower()}*"):
                        matches.append({
                            "path": str(rel_path),
                            "type": "name_match",
                        })
                        continue

                    # Content-based search
                    if content_search:
                        try:
                            if full_path.stat().st_size > self._max_file_size:
                                continue
                            file_content = full_path.read_text(encoding="utf-8", errors="replace")
                            if pattern in file_content:
                                # Find line numbers
                                line_numbers = [
                                    i + 1 for i, line in enumerate(file_content.splitlines())
                                    if pattern in line
                                ]
                                matches.append({
                                    "path": str(rel_path),
                                    "type": "content_match",
                                    "line_numbers": line_numbers[:10],  # Limit line numbers
                                })
                        except Exception:
                            continue

            return {
                "success": True,
                "pattern": pattern,
                "search_path": str(search_path),
                "content_search": content_search,
                "matches": matches,
                "total_matches": len(matches),
                "truncated": len(matches) >= max_results,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _directory_organize(self, task: TaskRequest) -> Dict[str, Any]:
        """Organize files in a directory based on rules."""
        source_dir = task.input_data.get("source_dir", ".")
        rules = task.input_data.get("rules", [])
        dry_run = task.input_data.get("dry_run", False)

        try:
            target_dir = self._resolve_path(source_dir)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        if not target_dir.exists() or not target_dir.is_dir():
            return {"success": False, "error": f"Directory not found: {source_dir}"}

        if not rules:
            # Default rules: organize by extension
            rules = [{"by": "extension"}]

        operations = []
        files_moved = 0

        try:
            for item in target_dir.iterdir():
                if not item.is_file():
                    continue

                for rule in rules:
                    dest_subdir = None
                    by = rule.get("by", "extension")

                    if by == "extension":
                        ext = item.suffix.lstrip(".") or "no_extension"
                        dest_subdir = ext.lower()
                    elif by == "name_pattern":
                        pattern = rule.get("pattern", "*")
                        if fnmatch.fnmatch(item.name, pattern):
                            dest_subdir = rule.get("destination", "matched")
                    elif by == "size":
                        size = item.stat().st_size
                        thresholds = rule.get("thresholds", {
                            "small": 1024 * 1024,
                            "medium": 10 * 1024 * 1024,
                        })
                        if size < thresholds.get("small", 1024 * 1024):
                            dest_subdir = "small"
                        elif size < thresholds.get("medium", 10 * 1024 * 1024):
                            dest_subdir = "medium"
                        else:
                            dest_subdir = "large"
                    elif by == "date":
                        mtime = datetime.fromtimestamp(item.stat().st_mtime, tz=timezone.utc)
                        dest_subdir = mtime.strftime("%Y-%m")

                    if dest_subdir:
                        dest_dir = target_dir / dest_subdir
                        dest_path = dest_dir / item.name

                        operations.append({
                            "source": str(item.relative_to(self._base_dir)),
                            "destination": str(dest_path.relative_to(self._base_dir)),
                            "rule": by,
                        })

                        if not dry_run:
                            dest_dir.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(item), str(dest_path))
                            files_moved += 1
                        break

            return {
                "success": True,
                "source_dir": str(source_dir),
                "operations": operations,
                "files_moved": files_moved,
                "dry_run": dry_run,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _file_watch(self, task: TaskRequest) -> Dict[str, Any]:
        """Watch files or directories for changes (placeholder)."""
        paths = task.input_data.get("paths", [])
        events = task.input_data.get("events", ["modify", "create", "delete"])
        watch_id = task.input_data.get("watch_id")

        if not paths:
            return {"success": False, "error": "No paths provided to watch"}

        if not watch_id:
            from uuid import uuid4
            watch_id = str(uuid4())

        resolved_paths = []
        for p in paths:
            try:
                resolved = self._resolve_path(p)
                resolved_paths.append(str(resolved))
            except ValueError as e:
                return {"success": False, "error": str(e)}

        self._watched_paths[watch_id] = {
            "paths": resolved_paths,
            "events": events,
            "created_at": self._now_iso(),
        }
        self.metadata["watched_paths_count"] = len(self._watched_paths)

        return {
            "success": True,
            "watch_id": watch_id,
            "paths": resolved_paths,
            "events": events,
            "message": "Watch registered. File watching is a placeholder; actual monitoring would use inotify/fsevents.",
        }

    def _now_iso(self) -> str:
        """Return current UTC time in ISO format."""
        return datetime.now(timezone.utc).isoformat()
