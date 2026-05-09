"""Code Agent - executes, analyzes, refactors, and tests code."""

import ast
import io
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional

from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus


class CodeAgent(BaseAgent):
    """Agent for safe code execution, analysis, refactoring, and testing."""

    def __init__(
        self,
        name: str = "code_agent",
        event_bus: EventBus = None,
        config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            name=name,
            agent_type="code",
            provider="local",
            event_bus=event_bus,
            config=config,
        )
        self.add_capability(
            AgentCapability(
                name="code_execution",
                description="Execute Python code safely in a sandboxed environment",
                parameters={"code": "str", "timeout": "int"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="code_analysis",
                description="Analyze code quality, complexity, and style",
                parameters={"code": "str", "analysis_type": "str"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="refactoring",
                description="Refactor and improve code structure",
                parameters={"code": "str", "refactor_type": "str"},
            )
        )
        self.add_capability(
            AgentCapability(
                name="test_runner",
                description="Run tests and report results",
                parameters={"test_code": "str", "test_framework": "str"},
            )
        )
        self._sandbox_dir = self.config.get("sandbox_dir", tempfile.gettempdir())
        self._max_output_size = self.config.get("max_output_size", 10000)
        self._execution_timeout = self.config.get("execution_timeout", 30)

    async def initialize(self) -> None:
        """Initialize the code agent."""
        os.makedirs(self._sandbox_dir, exist_ok=True)
        self.metadata["sandbox_dir"] = self._sandbox_dir
        self.metadata["initialized"] = True

    async def health_check(self) -> bool:
        """Check if the code execution environment is healthy."""
        try:
            test_code = "print('health_check_ok')"
            result = await self._execute_code(test_code)
            return result.get("success", False)
        except Exception:
            return False

    async def execute(self, task: TaskRequest) -> TaskResult:
        """Execute a code-related task."""
        task_type = task.input_data.get("task_type", "code_execution")

        handlers = {
            "code_execution": self._execute_code,
            "code_analysis": self._analyze_code,
            "refactoring": self._refactor_code,
            "test_runner": self._run_tests,
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

    async def _execute_code(self, task_or_code) -> Dict[str, Any]:
        """Execute Python code safely in a sandbox."""
        if isinstance(task_or_code, TaskRequest):
            code = task_or_code.input_data.get("code", "")
            timeout = task_or_code.input_data.get("timeout", self._execution_timeout)
        else:
            code = task_or_code
            timeout = self._execution_timeout

        if not code:
            return {"success": False, "error": "No code provided"}

        # Security: restrict builtins
        safe_builtins = {
            "abs": abs,
            "all": all,
            "any": any,
            "ascii": ascii,
            "bin": bin,
            "bool": bool,
            "bytearray": bytearray,
            "bytes": bytes,
            "chr": chr,
            "complex": complex,
            "dict": dict,
            "dir": dir,
            "divmod": divmod,
            "enumerate": enumerate,
            "filter": filter,
            "float": float,
            "format": format,
            "frozenset": frozenset,
            "hasattr": hasattr,
            "hash": hash,
            "hex": hex,
            "id": id,
            "int": int,
            "isinstance": isinstance,
            "issubclass": issubclass,
            "iter": iter,
            "len": len,
            "list": list,
            "map": map,
            "max": max,
            "min": min,
            "next": next,
            "oct": oct,
            "ord": ord,
            "pow": pow,
            "print": print,
            "range": range,
            "repr": repr,
            "reversed": reversed,
            "round": round,
            "set": set,
            "slice": slice,
            "sorted": sorted,
            "str": str,
            "sum": sum,
            "tuple": tuple,
            "type": type,
            "zip": zip,
        }

        old_stdout = sys.stdout
        old_stderr = sys.stderr
        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()

        try:
            sys.stdout = stdout_buffer
            sys.stderr = stderr_buffer

            compiled_code = compile(code, "<sandbox>", "exec")
            exec_globals = {"__builtins__": safe_builtins}
            exec(compiled_code, exec_globals)

            output = stdout_buffer.getvalue()
            errors = stderr_buffer.getvalue()

            if len(output) > self._max_output_size:
                output = output[: self._max_output_size] + "\n...[truncated]"

            return {
                "success": True,
                "output": output,
                "errors": errors,
                "truncated": len(stdout_buffer.getvalue()) > self._max_output_size,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

    async def _analyze_code(self, task: TaskRequest) -> Dict[str, Any]:
        """Analyze code quality and structure."""
        code = task.input_data.get("code", "")
        analysis_type = task.input_data.get("analysis_type", "general")

        if not code:
            return {"success": False, "error": "No code provided"}

        result = {"analysis_type": analysis_type, "issues": []}

        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return {"success": False, "error": f"Syntax error: {e}"}

        if analysis_type in ("general", "complexity"):
            # Simple complexity metrics
            func_count = len([n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)])
            class_count = len([n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)])
            import_count = len([n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))])

            lines = code.splitlines()
            blank_lines = len([l for l in lines if not l.strip()])
            comment_lines = len([l for l in lines if l.strip().startswith("#")])

            result["metrics"] = {
                "functions": func_count,
                "classes": class_count,
                "imports": import_count,
                "total_lines": len(lines),
                "blank_lines": blank_lines,
                "comment_lines": comment_lines,
                "code_lines": len(lines) - blank_lines - comment_lines,
            }

        if analysis_type in ("general", "style"):
            # Simple style checks
            lines = code.splitlines()
            for i, line in enumerate(lines, 1):
                if len(line) > 120:
                    result["issues"].append({
                        "line": i,
                        "type": "style",
                        "message": f"Line too long ({len(line)} > 120 characters)",
                    })
                if line.rstrip() != line:
                    result["issues"].append({
                        "line": i,
                        "type": "style",
                        "message": "Trailing whitespace",
                    })

        result["success"] = True
        return result

    async def _refactor_code(self, task: TaskRequest) -> Dict[str, Any]:
        """Refactor code based on specified type."""
        code = task.input_data.get("code", "")
        refactor_type = task.input_data.get("refactor_type", "format")

        if not code:
            return {"success": False, "error": "No code provided"}

        refactored = code
        changes = []

        if refactor_type in ("format", "all"):
            # Simple formatting: normalize whitespace
            lines = code.splitlines()
            formatted_lines = []
            prev_blank = False
            for line in lines:
                stripped = line.rstrip()
                if not stripped:
                    if not prev_blank:
                        formatted_lines.append("")
                        prev_blank = True
                else:
                    formatted_lines.append(stripped)
                    prev_blank = False
            refactored = "\n".join(formatted_lines)
            changes.append("Normalized whitespace and removed trailing whitespace")

        if refactor_type in ("imports", "all"):
            # Sort and deduplicate imports
            try:
                tree = ast.parse(refactored)
                imports = []
                other_lines = []
                for node in tree.body:
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        imports.append(ast.unparse(node))
                    else:
                        break
                if imports:
                    sorted_imports = sorted(set(imports))
                    refactored_lines = sorted_imports + [""] + refactored.splitlines()[len(imports):]
                    refactored = "\n".join(refactored_lines)
                    changes.append("Sorted and deduplicated imports")
            except Exception:
                pass

        return {
            "success": True,
            "original": code,
            "refactored": refactored,
            "changes": changes,
            "refactor_type": refactor_type,
        }

    async def _run_tests(self, task: TaskRequest) -> Dict[str, Any]:
        """Run tests using pytest or unittest."""
        test_code = task.input_data.get("test_code", "")
        test_framework = task.input_data.get("test_framework", "pytest")
        target_code = task.input_data.get("target_code", "")

        if not test_code:
            return {"success": False, "error": "No test code provided"}

        with tempfile.TemporaryDirectory() as tmpdir:
            # Write target code if provided
            if target_code:
                target_path = os.path.join(tmpdir, "target_module.py")
                with open(target_path, "w") as f:
                    f.write(target_code)

            # Write test code
            test_path = os.path.join(tmpdir, "test_module.py")
            with open(test_path, "w") as f:
                if target_code:
                    f.write("import sys\nsys.path.insert(0, '{}')\n".format(tmpdir))
                f.write(test_code)

            try:
                if test_framework == "pytest":
                    cmd = [sys.executable, "-m", "pytest", test_path, "-v", "--tb=short"]
                else:
                    cmd = [sys.executable, "-m", "unittest", "-v", test_path]

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self._execution_timeout,
                    cwd=tmpdir,
                )

                return {
                    "success": result.returncode == 0,
                    "returncode": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "framework": test_framework,
                }
            except subprocess.TimeoutExpired:
                return {"success": False, "error": "Tests timed out"}
            except Exception as e:
                return {"success": False, "error": str(e)}
